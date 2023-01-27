import typing

import logging
import contextvars
import contextlib
from . import debug_compile

from . import compile_domain
from . import op_args
from . import weave_types as types
from . import graph
from . import registry_mem
from . import dispatch
from . import graph_debug
from . import stitch
from . import compile_table
from . import weave_internal
from . import engine_trace
from . import errors

# These call_* functions must match the actual op implementations.
# But we don't want to import the op definitions themselves here, since
# those depend on the decorators, which aren't defined in the engine.

DEBUG_COMPILE = False


def _call_run_await(run_node: graph.Node) -> graph.OutputNode:
    run_node_type = typing.cast(types.RunType, run_node.type)
    return graph.OutputNode(run_node_type.output, "run-await", {"self": run_node})


# We don't want to import the op definitions themselves here, since
# those depend on the decorators, which aren't defined in the engine.
def _call_execute(function_node: graph.Node) -> graph.OutputNode:
    function_node_type = typing.cast(types.Function, function_node.type)
    return graph.OutputNode(
        function_node_type.output_type, "execute", {"node": function_node}
    )


def _dispatch_map_fn_refining(node: graph.Node) -> typing.Optional[graph.OutputNode]:
    if isinstance(node, graph.OutputNode):
        from_op = node.from_op
        try:
            op = dispatch.get_op_for_inputs(node.from_op.name, from_op.input_types)
            params = from_op.inputs
            if isinstance(op.input_type, op_args.OpNamedArgs):
                params = {
                    k: n
                    for k, n in zip(op.input_type.arg_types, from_op.inputs.values())
                }
            return op(**params)
        except errors.WeaveDispatchError:
            logging.error(
                "Error while dispatching name: %s for input types: %s.\n  Expression: %s",
                from_op.name,
                from_op.input_types,
                graph_debug.node_expr_str_full(node),
            )
            raise
    return None


def _remove_optional(t: types.Type) -> types.Type:
    if types.is_optional(t):
        return types.non_none(t)
    return t


def _dispatch_map_fn_no_refine(node: graph.Node) -> typing.Optional[graph.OutputNode]:
    if isinstance(node, graph.OutputNode):
        if node.from_op.name == "tag-indexCheckpoint":
            # I'm seeing that there is no indexCheckpoint tag present
            # on types that come from WeaveJS (at least by the time we call
            # this op). Maybe a WeaveJS bug?
            # TODO
            return node
        if node.from_op.name == "file-type":
            # since we didn't refine, the input to file-type is not correct yet.
            # if its in the graph, just trust that's what we want
            # TODO: does this work for mapped case?
            return node
        from_op = node.from_op
        op = dispatch.get_op_for_inputs(node.from_op.name, from_op.input_types)
        params = from_op.inputs
        if isinstance(op.input_type, op_args.OpNamedArgs):
            params = {
                k: n for k, n in zip(op.input_type.arg_types, from_op.inputs.values())
            }

        output_type = node.type
        # In the case where we are dispatching to a new op, we want to use the
        # new op's `unrefined_output_type_for_params` output type - rather than
        # blindly trusting the client type.
        if not node.from_op.name.startswith("local-artifact://") and (
            node.from_op.name != op.name
        ):
            output_type = op.unrefined_output_type_for_params(params)

        return graph.OutputNode(_remove_optional(output_type), op.uri, params)
    return None


def _make_auto_op_map_fn(when_type: type[types.Type], call_op_fn):
    def fn(node: graph.Node) -> typing.Optional[graph.Node]:
        if isinstance(node, graph.OutputNode):
            node_inputs = node.from_op.inputs
            op_def = registry_mem.memory_registry.get_op(node.from_op.name)
            if (
                op_def.name == "tag-indexCheckpoint"
                or op_def.name == "Object-__getattr__"
                or op_def.name == "set"
                # panel_scatter and panel_distribution have the incorrect
                # input types for their config arg. They should be weave.Node.
                # We need a frontend fix to handle that. For now there's a hack
                # here.
                # TODO: Fix in frontend and panel_* and remove this hack.
                or (
                    isinstance(op_def.concrete_output_type, types.Type)
                    and op_def.concrete_output_type._base_type is not None
                    and op_def.concrete_output_type._base_type.name == "Panel"
                )
            ):
                # These are supposed to be a passthrough op, we don't want to convert
                # it. TODO: Find a more general way, maybe by type inspection?
                return None
            new_inputs: dict[str, graph.Node] = {}
            swapped = False
            for k, input_node in node_inputs.items():
                actual_input_type = input_node.type
                new_inputs[k] = input_node
                if not isinstance(actual_input_type, when_type):
                    continue
                if isinstance(op_def.input_type, op_args.OpNamedArgs):
                    op_input_type = op_def.input_type.arg_types[k]
                elif isinstance(op_def.input_type, op_args.OpVarArgs):
                    op_input_type = op_def.input_type.arg_type
                else:
                    raise ValueError(
                        f"Unexpected op input type {op_def.input_type} for op {op_def.name}"
                    )
                if callable(op_input_type):
                    continue
                if not isinstance(op_input_type, when_type):
                    new_inputs[k] = call_op_fn(input_node)
                    swapped = True
            if swapped:
                return graph.OutputNode(node.type, node.from_op.name, new_inputs)
        return None

    return fn


_await_run_outputs_map_fn = _make_auto_op_map_fn(types.RunType, _call_run_await)

_execute_nodes_map_fn = _make_auto_op_map_fn(types.Function, _call_execute)


def compile_apply_column_pushdown(leaf_nodes: list[graph.Node]) -> list[graph.Node]:
    # This is specific to project-runs2 (not yet used in W&B production) for now. But it
    # is a general pattern that will work for all arrow tables.
    if not graph.filter_nodes_full(
        leaf_nodes,
        lambda n: isinstance(n, graph.OutputNode) and n.from_op.name == "project-runs2",
    ):
        return leaf_nodes

    p = stitch.stitch(leaf_nodes)

    def _replace_with_column_pushdown(node: graph.Node) -> graph.Node:
        if isinstance(node, graph.OutputNode) and node.from_op.name == "project-runs2":
            forward_obj = p.get_result(node)
            run_cols = compile_table.get_projection(forward_obj)
            config_cols = list(run_cols.get("config", {}).keys())
            summary_cols = list(run_cols.get("summary", {}).keys())
            return graph.OutputNode(
                node.type,
                "project-runs2_with_columns",
                {
                    "project": node.from_op.inputs["project"],
                    "config_cols": weave_internal.const(config_cols),
                    "summary_cols": weave_internal.const(summary_cols),
                },
            )
        return node

    return graph.map_nodes_full(leaf_nodes, _replace_with_column_pushdown)


def compile_fix_calls(nodes: typing.List[graph.Node]) -> typing.List[graph.Node]:
    return graph.map_nodes_full(nodes, _dispatch_map_fn_no_refine)


def compile_await(nodes: typing.List[graph.Node]) -> typing.List[graph.Node]:
    return graph.map_nodes_full(nodes, _await_run_outputs_map_fn)


def compile_execute(nodes: typing.List[graph.Node]) -> typing.List[graph.Node]:
    return graph.map_nodes_full(nodes, _execute_nodes_map_fn)


def compile_refine(nodes: typing.List[graph.Node]) -> typing.List[graph.Node]:
    return graph.map_nodes_full(nodes, _dispatch_map_fn_refining)


def _compile(nodes: typing.List[graph.Node]) -> typing.List[graph.Node]:
    tracer = engine_trace.tracer()
    # logging.info("Starting compilation of graph with %s leaf nodes" % len(nodes))

    n = nodes

    # If we're being called from WeaveJS, we need to use dispatch to determine
    # which ops to use. Critically, this first phase does not actually refine
    # op output types, so after this, the types in the graph are not yet correct.
    with tracer.trace("compile:fix_calls"):
        n = compile_fix_calls(n)

    # Now that we have the correct calls, we can do our forward-looking pushdown
    # optimizations. These do not depend on having correct types in the graph.
    with tracer.trace("compile:gql"):
        n = compile_domain.apply_domain_op_gql_translation(n)
    with tracer.trace("compile:column_pushdown"):
        n = compile_apply_column_pushdown(n)

    # Auto-transforms, where we insert operations to convert between types
    # as needed.
    # TODO: is it ok to have this before final refine?
    with tracer.trace("compile:await"):
        n = compile_await(n)
    with tracer.trace("compile:execute"):
        n = compile_execute(n)

    # Final refine, to ensure the graph types are exactly what Weave python
    # produces. This phase can execute parts of the graph. It's very important
    # that this is the final phase, so that when we execute the rest of the
    # graph, we reuse any results produced in this phase, instead of re-executing
    # those nodes.
    with tracer.trace("compile:refine"):
        n = compile_refine(n)

    # This is very expensive!
    # loggable_nodes = graph_debug.combine_common_nodes(n)
    # logging.info(
    #     "Compilation complete. Result nodes:\n%s",
    #     "\n".join(graph_debug.node_expr_str_full(n) for n in loggable_nodes),
    # )

    if DEBUG_COMPILE:
        debug_compile.check_weave0_compile_result(nodes, n)

    return n


_currently_compiling: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "_current_compiling", default=False
)


def _is_compiling() -> bool:
    return _currently_compiling.get()


@contextlib.contextmanager
def _compiling():
    token = _currently_compiling.set(True)
    try:
        yield
    finally:
        _currently_compiling.reset(token)


def compile(nodes: typing.List[graph.Node]) -> typing.List[graph.Node]:
    """
    This method is used to "compile" a list of nodes. Here we can add any
    optimizations or graph rewrites
    """
    # The refine phase may execute parts of the graph. Executing recursively
    # calls compile. Use context to ensure we only compile the top level
    # graph.
    if _is_compiling():
        return nodes
    with _compiling():
        return _compile(nodes)
