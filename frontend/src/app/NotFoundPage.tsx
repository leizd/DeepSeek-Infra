import { Link } from "react-router-dom";

import { appRoutes } from "./routes";

export function NotFoundPage() {
  return (
    <main className="route-not-found">
      <p>404</p>
      <h1>Page not found</h1>
      <Link to={appRoutes.root}>Return to chat</Link>
    </main>
  );
}
