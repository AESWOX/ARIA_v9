import { Component, type ErrorInfo, type ReactNode } from "react";

/* Страница не должна «гаснуть» при ошибке рендера: показываем текст ошибки и
   кнопку перезагрузки. resetKey (например pathname) сбрасывает состояние при
   переходе на другую страницу. */

type Props = { children: ReactNode; resetKey?: string };
type State = { error: Error | null };

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // Видно в DevTools и в логах webview.
    console.error("[ErrorBoundary]", error, info.componentStack);
  }

  componentDidUpdate(prev: Props) {
    if (this.state.error && prev.resetKey !== this.props.resetKey) {
      this.setState({ error: null });
    }
  }

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;
    return (
      <div className="m-4 space-y-2 border border-destructive/50 bg-destructive/10 p-3 text-xs">
        <div className="font-medium text-destructive">Page crashed: {error.name}: {error.message}</div>
        {error.stack && (
          <pre className="max-h-64 overflow-auto whitespace-pre-wrap text-muted-foreground">
            {error.stack.split("\n").slice(0, 8).join("\n")}
          </pre>
        )}
        <button
          type="button"
          className="border border-border px-2 py-1 hover:bg-foreground/5"
          onClick={() => window.location.reload()}
        >
          Reload
        </button>
      </div>
    );
  }
}

export default ErrorBoundary;
