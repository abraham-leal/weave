import typing

import weave
from .. import dispatch
from .. import weave_internal as internal
from .. import weave_types as types
from .. import weave_internal
from ..panels import panel_group
from ..panels import panel_board
from ..panels_py import panel_autoboard
from .generator_templates import template_registry


panels = weave.panels
ops = weave.ops


# BOARD_ID must be unique across all ops. It must only contain letters and underscores.
BOARD_ID = "open_ai_completions_monitor"

# BOARD_DISPLAY_NAME is the name that will be displayed in the UI
BOARD_DISPLAY_NAME = "OpenAI Monitor Board"

# BOARD_DESCRIPTION is the description that will be displayed in the UI
BOARD_DESCRIPTION = "Monitor OpenAI Completions"

# BOARD_INPUT_WEAVE_TYPE is the weave type of the input node.
BOARD_INPUT_WEAVE_TYPE = types.List(
    types.TypedDict(
        {
            "inputs": types.optional(
                types.TypedDict(
                    {
                        "messages": types.optional(
                            types.List(
                                types.TypedDict(
                                    {
                                        "role": types.optional(types.String()),
                                        "content": types.optional(types.String()),
                                    }
                                )
                            )
                        )
                    }
                )
            ),
            "output": types.optional(
                types.TypedDict(
                    {
                        "id": types.optional(types.String()),
                        "object": types.optional(types.String()),
                        "model": types.optional(types.String()),
                        "usage": types.optional(
                            types.TypedDict(
                                {
                                    "prompt_tokens": types.optional(types.Number()),
                                    "completion_tokens": types.optional(types.Number()),
                                    "total_tokens": types.optional(types.Number()),
                                }
                            )
                        ),
                        "choices": types.optional(
                            types.List(
                                types.TypedDict(
                                    {
                                        "message": types.optional(
                                            types.TypedDict(
                                                {
                                                    "role": types.optional(
                                                        types.String()
                                                    ),
                                                    "content": types.optional(
                                                        types.String()
                                                    ),
                                                }
                                            )
                                        ),
                                        "finish_reason": types.optional(types.String()),
                                    }
                                )
                            )
                        ),
                    }
                )
            ),
            "timestamp": types.optional(types.Timestamp()),
        }
    )
)


board_name = "py_board-" + BOARD_ID


# @weave.type()
# class OpenAITable(weave.Panel):
#     id = "OpenAITable"
#     input_node: weave.Node[list[typing.Any]]

#     @weave.op()
#     def render(self) -> weave.panels.Table:
#         table = panels.Table(
#             self.input_node,
#             columns=[lambda row: row["inputs"]["messages"][-1]["content"]],
#         )
#         # table.add_column( lambda row: row["inputs"]["messages"][-1]["content"], "Messages"
#         # )
#         # table.add_column(lambda row: row["messages"][-1]["content"], "Message")
#         return table


def cost(row: dispatch.RuntimeOutputNode) -> dispatch.RuntimeOutputNode:
    return row["output.usage.total_tokens"] * 0.0015e-3


@weave.op(  # type: ignore
    name=board_name,
    hidden=True,
    input_type={
        "input_node": types.Function(
            {},
            BOARD_INPUT_WEAVE_TYPE,
        )
    },
)
def board(
    input_node,
) -> panels.Group:
    timestamp_col_name = "timestamp"

    ### Varbar

    # Add the input node as raw data
    varbar = panel_board.varbar()

    dataset = varbar.add("dataset", input_node)

    # Setup date range variables:
    ## 1. raw_data_range is derived from raw_data
    dataset_range = varbar.add(
        "dataset_range",
        weave.ops.make_list(
            a=dataset[timestamp_col_name].min(),
            b=dataset[timestamp_col_name].max(),
        ),
        hidden=True,
    )

    ## 2. user_zoom_range is used to store the user's zoom range
    user_zoom_range = varbar.add("user_zoom_range", None, hidden=True)

    ## 2.b: Setup a date picker to set the user_zoom_range
    varbar.add(
        "date_picker",
        weave.panels.DateRange(user_zoom_range, domain=dataset[timestamp_col_name]),
    )

    ## 3. bin_range is derived from user_zoom_range and raw_data_range. This is
    ##    the range of data that will be displayed in the charts.
    bin_range = varbar.add(
        "bin_range", user_zoom_range.coalesce(dataset_range), hidden=True
    )

    # clean_data = varbar.add(
    #     "clean_data",
    #     dataset.map(
    #         lambda row: ops.dict_(
    #             id=row["output"]["id"],
    #             object=row["output"]["object"],
    #             model=row["output"]["model"],
    #             messages=row["inputs"]["messages"],
    #             usage=row["output"]["usage"],
    #             completion=row["output"]["choices"][0]["message"],
    #             finish_reason=row["output"]["choices"][0]["finish_reason"],
    #             timestamp=row["timestamp"],
    #             latency_ms=row["end_time_ms"] - row["start_time_ms"],
    #         )
    #     ),
    #     hidden=True,
    # )

    # Derive the windowed data to use in the plots as a function of bin_range

    window_data = varbar.add(
        "window_data",
        dataset.filter(
            lambda row: weave.ops.Boolean.bool_and(
                row[timestamp_col_name] >= bin_range[0],
                row[timestamp_col_name] <= bin_range[1],
            )
        ),
        hidden=True,
    )

    filter_fn = varbar.add(
        "filter_fn",
        weave_internal.define_fn(
            {"row": input_node.type.object_type}, lambda row: weave_internal.const(True)
        ),
        hidden=True,
    )
    filter_editor = varbar.add(
        "filter_editor",
        weave.panels.FilterEditor(filter_fn, node=window_data),
    )

    filtered_data = varbar.add("filtered_data", dataset.filter(filter_fn), hidden=True)

    filtered_window_data = varbar.add(
        "filtered_window_data", window_data.filter(filter_fn), hidden=True
    )

    groupby = varbar.add("groupby", "output.model", hidden=True)
    groupby_dropdown = varbar.add(
        "groupby_dropdown",
        weave.panels.Dropdown(
            groupby,
            choices=weave.ops.List.concat(
                weave.ops.make_list(
                    a=weave_internal.const(["output.model"]),
                    b=dataset["attributes"]
                    .keys()
                    .flatten()
                    .unique()
                    .map(lambda k: weave_internal.const("attributes.") + k),
                )
            ),
        ),
    )

    height = 5

    ### Overview tab

    overview_tab = weave.panels.Group(
        layoutMode="grid",
        showExpressions=True,
        enableAddPanel=True,
    )  # , showExpressions="titleBar")
    overview_tab.add(
        "request_count",
        panel_autoboard.timeseries_count_bar(
            filtered_data,
            bin_domain_node=bin_range,
            x_axis_key="timestamp",
            groupby_key=groupby,
            x_domain=user_zoom_range,
            n_bins=100,
        ),
        layout=weave.panels.GroupPanelLayout(x=0, y=0, w=24, h=height),
    )

    overview_tab.add(
        "cost",
        panel_autoboard.timeseries(
            filtered_data,
            bin_domain_node=bin_range,
            x_axis_key="timestamp",
            y_expr=lambda row: cost(row).sum(),
            y_title="total cost ($)",
            groupby_key=groupby,
            x_domain=user_zoom_range,
            n_bins=50,
        ),
        layout=weave.panels.GroupPanelLayout(x=0, y=height, w=12, h=height),
    )

    # latency
    overview_tab.add(
        "latency",
        panel_autoboard.timeseries(
            filtered_data,
            bin_domain_node=bin_range,
            x_axis_key="timestamp",
            y_expr=lambda row: (row["end_time_ms"].avg() - row["start_time_ms"].avg())
            / 1000,
            y_title="avg latency (s)",
            groupby_key=groupby,
            x_domain=user_zoom_range,
            n_bins=50,
        ),
        layout=weave.panels.GroupPanelLayout(x=12, y=height, w=12, h=height),
    )

    overview_tab.add(
        "avg cost per req",
        cost(filtered_window_data).avg(),  # type: ignore
        layout=weave.panels.GroupPanelLayout(x=0, y=height * 2, w=6, h=height),
    )
    overview_tab.add(
        "avg prompt tokens per req",
        filtered_window_data["output.usage.prompt_tokens"].avg(),  # type: ignore
        layout=weave.panels.GroupPanelLayout(x=6, y=height * 2, w=6, h=height),
    )
    overview_tab.add(
        "avg completion tokens per req",
        filtered_window_data["output.usage.completion_tokens"].avg(),  # type: ignore
        layout=weave.panels.GroupPanelLayout(x=12, y=height * 2, w=6, h=height),
    )
    overview_tab.add(
        "avg total tokens per req",
        filtered_window_data["output.usage.total_tokens"].avg(),  # type: ignore
        layout=weave.panels.GroupPanelLayout(x=18, y=height * 2, w=6, h=height),
    ),

    # Show a plot for each attribute.
    # TODO: This doesn't really work yet (needs some manual UI configuration currently,
    # and it's ugly).
    # overview_tab.add(
    #     "attributes", weave.panels.EachColumn(filtered_window_data["attributes"])
    # )

    ### Requests tab

    # requests_tab = weave.panels.Group(
    #     layoutMode="grid",
    #     showExpressions=True,
    # )  # l, showExpressions="titleBar")

    requests_table = panels.Table(filtered_window_data)  # type: ignore
    requests_table.add_column(lambda row: row["output.model"], "Model")
    requests_table.add_column(
        lambda row: row["inputs.messages"][-1]["content"], "Message"
    )
    requests_table.add_column(
        lambda row: row["output.choices"][-1]["message.content"], "Completion"
    )
    requests_table.add_column(
        lambda row: row["output.usage.prompt_tokens"], "Prompt Tokens"
    )
    requests_table.add_column(
        lambda row: row["output.usage.completion_tokens"], "Completion Tokens"
    )
    requests_table.add_column(
        lambda row: row["output.usage.total_tokens"], "Total Tokens"
    )
    requests_table.add_column(lambda row: row["latency_ms"], "Latency")
    requests_table.add_column(lambda row: row["timestamp"], "Timestamp")

    requests_table_var = overview_tab.add(
        "table",
        requests_table,
        layout=weave.panels.GroupPanelLayout(x=0, y=15, w=24, h=8),
    )
    overview_tab.add(
        "input",
        panels.Table(  # type: ignore
            requests_table_var.active_data()["inputs.messages"],
            columns=[lambda row: row["role"], lambda row: row["content"]],
        ),
        layout=weave.panels.GroupPanelLayout(x=0, y=23, w=12, h=8),
    )
    overview_tab.add(
        "output",
        requests_table_var.active_row(),
        layout=weave.panels.GroupPanelLayout(x=12, y=23, w=12, h=8),
    )

    # attributes_tab = weave.panels.Group(layoutMode="grid")

    # users_tab = weave.panels.Group(layoutMode="grid")

    # models_tab = weave.panels.Group(layoutMode="grid")

    # tabs = panels.Group(
    #     layoutMode="tab",
    #     items={
    #         "Overview": overview_tab,
    #         "Requests": requests_tab,
    #         # "Attributes": attributes_tab,
    #         # "Users": users_tab,
    #         # "Models": models_tab,
    #     },
    # )

    return panels.Board(vars=varbar, panels=overview_tab)


template_registry.register(
    board_name,
    BOARD_DISPLAY_NAME,
    BOARD_DESCRIPTION,
)
