import { useCouncil } from "./useCouncil";
import { Header } from "./components/Header";
import { StatBand } from "./components/StatBand";
import { ImprovementChart } from "./components/ImprovementChart";
import { ActivityFeed } from "./components/ActivityFeed";
import { ReviewPanel } from "./components/ReviewPanel";
import { HeuristicsCard } from "./components/HeuristicsCard";
import { Toasts } from "./components/Toasts";

export default function App() {
  const { data, connected } = useCouncil();

  return (
    <div className="min-h-screen bg-background text-foreground">
      <Header data={data} connected={connected} />
      <Toasts data={data} />

      <main className="mx-auto max-w-[1400px] px-6 pb-24">
        <section className="pt-12 md:pt-16">
          <p className="text-sm font-medium uppercase tracking-widest text-muted-foreground">
            Recursive peer review
          </p>
          <h1 className="mt-3 max-w-3xl text-balance text-4xl font-medium leading-[1.08] tracking-tight md:text-6xl">
            A critic that gets{" "}
            <span className="italic underline decoration-1 underline-offset-8">measurably</span>{" "}
            better at reviewing your code.
          </h1>
        </section>

        <StatBand data={data} />

        <div className="mt-6 grid gap-4 lg:grid-cols-5">
          <div className="lg:col-span-3">
            <ImprovementChart data={data} />
          </div>
          <div className="lg:col-span-2">
            <ActivityFeed data={data} />
          </div>
        </div>

        <div className="mt-4 grid gap-4 lg:grid-cols-5">
          <div className="lg:col-span-3">
            <ReviewPanel data={data} />
          </div>
          <div className="lg:col-span-2">
            <HeuristicsCard data={data} />
          </div>
        </div>
      </main>

      <footer className="border-t border-border">
        <div className="mx-auto flex max-w-[1400px] items-center justify-between px-6 py-8 text-xs text-muted-foreground">
          <span>CodeCouncil · observer → critic → hooks → reflector</span>
          <span className="font-mono">{data ? data.repo.path : "connecting…"}</span>
        </div>
      </footer>
    </div>
  );
}
