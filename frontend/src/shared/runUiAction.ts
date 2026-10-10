export interface UiActionHandlers<T> {
  onSuccess?(value: T): void;
  onSettled?(): void;
}

export function runUiAction<T>(action: Promise<T>, handlers: UiActionHandlers<T> = {}): void {
  void action
    .then((value) => handlers.onSuccess?.(value))
    .catch(() => {
      // The controller and its Mutation state own the user-facing error.
    })
    .finally(() => handlers.onSettled?.());
}
