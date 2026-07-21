import fs from "node:fs";
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
      // audit trail: the exact prompt behind a verdict, by verdict id
      server.middlewares.use("/api/prompt", (req, res) => {
        const id = (req.url ?? "").replace(/^\//, "").split("?")[0];
        res.setHeader("Content-Type", "text/plain; charset=utf-8");
        if (!/^[a-f0-9]{6,32}$/.test(id)) {
          res.statusCode = 400;
          res.end("bad id");
          return;
        }
        const file = path.join(WATCHED_REPO, ".codecouncil", "prompts", `${id}.txt`);
        try {
          res.end(fs.readFileSync(file, "utf-8"));
        } catch {
          res.statusCode = 404;
          res.end("no prompt recorded for this verdict");
        }
      });
      // human-facing session receipt (Task 10), by file name
      server.middlewares.use("/api/receipt", (req, res) => {
        let name = (req.url ?? "").replace(/^\//, "").split("?")[0];
        try {
          name = decodeURIComponent(name);
        } catch {
          /* malformed percent-encoding — fall through to validation below */
        }
        res.setHeader("Content-Type", "text/plain; charset=utf-8");
        if (!name || name.includes("/") || name.includes("\\") || name.includes("..") || !name.endsWith(".md")) {
          res.statusCode = 400;
          res.end("bad name");
          return;
        }
        const file = path.join(WATCHED_REPO, ".codecouncil", "receipts", name);
        try {
          res.end(fs.readFileSync(file, "utf-8"));
        } catch {
          res.statusCode = 404;
          res.end("no receipt found with that name");
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
