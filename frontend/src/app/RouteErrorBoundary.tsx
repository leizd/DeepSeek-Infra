import { Component, type ErrorInfo, type ReactNode } from "react";
import { Link } from "react-router-dom";

import { appRoutes } from "./routes";

interface RouteErrorBoundaryProps {
  children: ReactNode;
}

interface RouteErrorBoundaryState {
  error: Error | null;
}

export class RouteErrorBoundary extends Component<RouteErrorBoundaryProps, RouteErrorBoundaryState> {
  state: RouteErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): RouteErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("Route render failed", error, info.componentStack);
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <main className="route-error" role="alert">
        <p>ROUTE ERROR</p>
        <h1>This page could not be opened</h1>
        <span>{this.state.error.message || "An unexpected frontend error occurred."}</span>
        <div>
          <Link to={appRoutes.root}>Return to chat</Link>
          <button type="button" onClick={() => window.location.reload()}>Reload page</button>
        </div>
      </main>
    );
  }
}
