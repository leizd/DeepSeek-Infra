/// <reference types="vite/client" />

declare const __APP_BUILD_ID__: string;
declare const __APP_SOURCE_REVISION__: string;

declare module "*?workspace-retry" {
  const component: import("react").ComponentType;
  export default component;
}
