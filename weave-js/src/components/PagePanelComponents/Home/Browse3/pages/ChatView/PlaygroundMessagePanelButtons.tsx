import {Button} from '@wandb/weave/components/Button';
import React from 'react';

import {usePlaygroundContext} from '../PlaygroundPage/PlaygroundContext';

type PlaygroundMessagePanelButtonsProps = {
  index: number;
  choiceIndex?: number;
  isTool: boolean;
  hasContent: boolean;
  contentRef: React.RefObject<HTMLDivElement>;
  setEditorHeight: (height: number | null) => void;
  responseIndexes?: number[];
};

export const PlaygroundMessagePanelButtons: React.FC<
  PlaygroundMessagePanelButtonsProps
> = ({
  index,
  choiceIndex,
  isTool,
  hasContent,
  contentRef,
  setEditorHeight,
  responseIndexes,
}) => {
  const {deleteMessage, deleteChoice, retry} = usePlaygroundContext();

  return (
    <div className="ml-auto flex gap-4 rounded-lg pt-[8px]">
      <Button
        variant="quiet"
        size="small"
        startIcon="randomize-reset-reload"
        onClick={() => retry?.(index, choiceIndex)}
        tooltip={
          !hasContent
            ? 'We currently do not support retrying functions'
            : 'Retry'
        }
        disabled={!hasContent}>
        Retry
      </Button>
      <Button
        variant="quiet"
        size="small"
        startIcon="pencil-edit"
        onClick={() => {
          setEditorHeight(
            contentRef?.current?.clientHeight
              ? // Accounts for padding and save buttons
                contentRef.current.clientHeight - 56
              : null
          );
        }}
        tooltip={
          !hasContent ? 'We currently do not support editing functions' : 'Edit'
        }
        disabled={!hasContent}>
        Edit
      </Button>
      <Button
        variant="quiet"
        size="small"
        startIcon="delete"
        onClick={() => {
          if (choiceIndex !== undefined) {
            deleteChoice?.(index, choiceIndex);
          } else {
            deleteMessage?.(index, responseIndexes);
          }
        }}
        tooltip={isTool ? 'Tool responses cannot be deleted' : 'Delete message'}
        disabled={isTool}>
        Delete
      </Button>
    </div>
  );
};
