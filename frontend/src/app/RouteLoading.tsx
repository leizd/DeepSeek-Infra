export function RouteLoading({ label = "Loading page..." }: { label?: string }) {
  return (
    <main className="route-loading" role="status">
      <span aria-hidden="true" />
      <strong>{label}</strong>
    </main>
  );
}
