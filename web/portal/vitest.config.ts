import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "jsdom",
    include: ["app/**/*.test.tsx"],
  },
  esbuild: { jsx: "automatic" },
});
