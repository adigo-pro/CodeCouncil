import path from "node:path";
import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { aggregate } from "./server/council";

// Repo whose .codecouncil/ the dashboard watches. Defaults to the CodeCouncil
// repo itself (ui/'s parent) — override with COUNCIL_REPO=/path/to/repo.
const WATCHED_REPO = path.resolve(__dirname, process.env.COUNCIL_REPO ?? "..");

function councilApi(): Plugin {
  return {
    name: "council-api",
    configureServer(server) {
      server.middlewares.use("/api/council", (_req, res) => {
        try {
          res.setHeader("Content-Type", "application/json");
          res.end(JSON.stringify(aggregate(WATCHED_REPO)));
        } catch (e) {
          res.statusCode = 500;
          res.end(JSON.stringify({ error: String(e) }));
        }
      });
    },
  };
}

export default defineConfig({
  plugins: [react(), tailwindcss(), councilApi()],
  resolve: {
    alias: { "@": path.resolve(__dirname, "src") },
  },
});
