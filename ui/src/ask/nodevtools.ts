// Skip the OpenUI dev-mode devtools overlay (its "share your app" toast covers the demo). Import before react-lang.
(globalThis as Record<symbol, unknown>)[Symbol.for("openui.devtools.autoMount")] = true;
export {};
