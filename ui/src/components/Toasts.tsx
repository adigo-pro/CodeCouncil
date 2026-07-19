import type { Council } from "../types";
import { useToasts, type Toast } from "../live";

const DOT: Record<Toast["kind"], string> = {
  pass: "border border-foreground/30",
  suggestion: "bg-foreground",
  error: "bg-bad",
  grade: "bg-ok",
  rewrite: "bg-foreground",
};

function ToastCard({ t }: { t: Toast }) {
  return (
    <div className="toast-in pointer-events-auto w-[340px] rounded-2xl border border-border bg-background/95 p-4 shadow-lg shadow-black/5 backdrop-blur">
      <div className="flex items-center gap-2.5">
        <span className={`h-2 w-2 shrink-0 rounded-full ${DOT[t.kind]}`} />
        <span className="text-[13px] font-semibold tracking-tight">{t.title}</span>
      </div>
      {t.body && (
        <p className="mt-1.5 line-clamp-3 pl-[18px] text-xs leading-relaxed text-muted-foreground">
          {t.body}
        </p>
      )}
    </div>
  );
}

export function Toasts({ data }: { data: Council | null }) {
  const toasts = useToasts(data);
  if (!toasts.length) return null;
  return (
    <div className="pointer-events-none fixed bottom-6 right-6 z-[100] flex flex-col gap-2.5">
      {toasts.map((t) => (
        <ToastCard key={t.id} t={t} />
      ))}
    </div>
  );
}
