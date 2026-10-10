export type IconName =
  | "menu"
  | "settings"
  | "close"
  | "back"
  | "check"
  | "error"
  | "plus"
  | "refresh"
  | "search"
  | "attach";

const PATHS: Record<IconName, string[]> = {
  menu: ["M3 4.5h10", "M3 8h10", "M3 11.5h10"],
  settings: [
    "M8 5.5a2.5 2.5 0 1 0 0 5 2.5 2.5 0 0 0 0-5Z",
    "M8 1.8v1.7", "M8 12.5v1.7", "M1.8 8h1.7", "M12.5 8h1.7",
    "M3.6 3.6l1.2 1.2", "M11.2 11.2l1.2 1.2", "M12.4 3.6l-1.2 1.2", "M4.8 11.2l-1.2 1.2",
  ],
  close: ["M4 4l8 8", "M12 4l-8 8"],
  back: ["M9.5 3.5 5 8l4.5 4.5"],
  check: ["M2.8 8.5 6 11.7 13.2 4.3"],
  error: ["M8 14a6 6 0 1 0 0-12 6 6 0 0 0 0 12Z", "M8 4.8v3.4", "M8 11h.01"],
  plus: ["M8 3v10", "M3 8h10"],
  refresh: ["M12.5 5.5A5 5 0 1 0 13 9", "M12.5 2.5v3h-3"],
  search: ["M7 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8Z", "M10.2 10.2l3.3 3.3"],
  attach: [
    "M12.8 6.2 7.6 11.4a2.12 2.12 0 0 1-3-3l5-5a3.54 3.54 0 0 1 5 5l-5.3 5.3a4.95 4.95 0 0 1-7-7l5-5",
  ],
};

export function Icon({ name, size = 16 }: { name: IconName; size?: number }) {
  return (
    <svg
      className={`icon icon--${name}`}
      width={size}
      height={size}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.5}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {PATHS[name].map((d) => (
        <path key={d} d={d} />
      ))}
    </svg>
  );
}
