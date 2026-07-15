/**
 * CSP-safe replacement for react-style-singleton.
 *
 * EcoreX's document and body are permanently viewport-locked by layout.css;
 * scrollable work areas live below that boundary. Radix Dialog therefore does
 * not need react-remove-scroll's dynamically generated body stylesheet. Keeping
 * this adapter at the bundler boundary preserves Radix's focus scope, aria
 * contract and dismissable layer while making runtime style injection
 * impossible under `style-src 'self'`.
 */

interface SingletonStyleProps {
  styles: string;
  dynamic?: unknown;
}

interface StylesheetSingleton {
  add: (styles: string) => void;
  remove: () => void;
}

export function styleSingleton(): (props: SingletonStyleProps) => null {
  return function CspStaticStyleBoundary(_props: SingletonStyleProps): null {
    return null;
  };
}

export function stylesheetSingleton(): StylesheetSingleton {
  return {
    add: (_styles: string) => undefined,
    remove: () => undefined,
  };
}

export function styleHookSingleton(): (_styles: string) => void {
  return function useCspStaticStyle(_styles: string): void {
    // Intentionally static: layout.css owns the scroll boundary.
  };
}
