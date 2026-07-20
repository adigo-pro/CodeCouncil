/**
 * Registers NVIDIA's hosted inference API (integrate.api.nvidia.com, OpenAI-
 * compatible) as a pi provider named "nvidia-nim", so --model
 * nvidia-nim/nvidia/<model> works. Model `id` must be NVIDIA's full catalog
 * string (e.g. "nvidia/nemotron-3-super-120b-a12b") — pi sends `id` verbatim
 * as the request's "model" field, and NVIDIA 404s on anything shorter.
 *
 * No secret lives here: the key is read from NVIDIA_API_KEY at request time
 * (no leading '$' — pi's apiKey field takes a bare env var name, confirmed
 * against pi's own bundled custom-provider example). Set it in
 * ~/.codecouncil/env (outside any watched repo) — see critic/agent.py.
 */
export default function (pi) {
  pi.registerProvider("nvidia-nim", {
    baseUrl: "https://integrate.api.nvidia.com/v1",
    apiKey: "NVIDIA_API_KEY",
    api: "openai-completions",
    models: [
      {
        id: "nvidia/nemotron-3-super-120b-a12b",
        name: "Nemotron 3 Super 120B",
        reasoning: true,
        input: ["text"],
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
        contextWindow: 128000,
        maxTokens: 8192,
      },
      {
        id: "nvidia/nemotron-3-ultra-550b-a55b",
        name: "Nemotron 3 Ultra 550B",
        reasoning: true,
        input: ["text"],
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
        contextWindow: 128000,
        maxTokens: 8192,
      },
      {
        id: "nvidia/nemotron-nano-3-30b-a3b",
        name: "Nemotron Nano 3 30B",
        reasoning: true,
        input: ["text"],
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
        contextWindow: 128000,
        maxTokens: 8192,
      },
    ],
  });
}
