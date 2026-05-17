"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Navbar from "@/components/Navbar";
import Footer from "@/components/Footer";
import { GitPullRequest, CheckCircle, AlertCircle, Clock, ExternalLink, LogOut, Plus } from "lucide-react";

const API = "https://graperoot-review-production.up.railway.app";
const GH_APP = "https://github.com/apps/graperoot-review/installations/new/permissions?target_id=84341876";

interface Stats {
  reviews_this_month: number;
  monthly_limit: number | null;
  total_reviews: number;
  repos_count: number;
}

interface Review {
  id: string;
  pr_title: string;
  repo: string;
  pr_url: string;
  findings: number;
  status: "completed" | "pending" | "failed";
  created_at: string;
}

function StatCard({ label, value, sub }: { label: string; value: string | number; sub?: string }) {
  return (
    <div className="rounded-xl border border-zinc-800 bg-[#101015] px-5 py-4">
      <p className="text-[11px] font-semibold text-zinc-500 uppercase tracking-widest mb-2">{label}</p>
      <p className="text-2xl font-bold text-white">{value}</p>
      {sub && <p className="text-xs text-zinc-500 mt-1">{sub}</p>}
    </div>
  );
}

function StatusBadge({ status }: { status: Review["status"] }) {
  if (status === "completed") return (
    <span className="inline-flex items-center gap-1 text-xs text-emerald-400">
      <CheckCircle size={11} /> Done
    </span>
  );
  if (status === "pending") return (
    <span className="inline-flex items-center gap-1 text-xs text-amber-400">
      <Clock size={11} /> Pending
    </span>
  );
  return (
    <span className="inline-flex items-center gap-1 text-xs text-red-400">
      <AlertCircle size={11} /> Failed
    </span>
  );
}

function timeAgo(iso: string) {
  const diff = Date.now() - new Date(iso).getTime();
  const m = Math.floor(diff / 60000);
  if (m < 60)  return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24)  return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

export default function DashboardPage() {
  const router = useRouter();
  const [user,    setUser]    = useState<string | null>(null);
  const [stats,   setStats]   = useState<Stats | null>(null);
  const [reviews, setReviews] = useState<Review[]>([]);
  const [loading, setLoading] = useState(true);
  const [error,   setError]   = useState(false);

  useEffect(() => {
    const storedUser = localStorage.getItem("gr_user");
    if (!storedUser) {
      router.replace("/login");
      return;
    }
    setUser(storedUser);

    const token = localStorage.getItem("gr_token");
    const headers: HeadersInit = token ? { Authorization: `Bearer ${token}` } : {};

    Promise.all([
      fetch(`${API}/api/stats`,   { headers }).then(r => r.ok ? r.json() : null),
      fetch(`${API}/api/reviews`, { headers }).then(r => r.ok ? r.json() : []),
    ])
      .then(([s, r]) => {
        if (s) setStats(s);
        if (Array.isArray(r)) setReviews(r);
      })
      .catch(() => setError(true))
      .finally(() => setLoading(false));
  }, [router]);

  function logout() {
    localStorage.removeItem("gr_user");
    localStorage.removeItem("gr_token");
    router.push("/");
  }

  if (!user) return null;

  const usageLabel = stats
    ? stats.monthly_limit
      ? `${stats.reviews_this_month} of ${stats.monthly_limit} this month`
      : "Unlimited"
    : "—";

  return (
    <>
      <Navbar />
      <main className="min-h-screen pt-24 pb-20 px-4 sm:px-6">
        <div className="max-w-5xl mx-auto">

          {/* Header */}
          <div className="flex items-start justify-between mb-8 gap-4">
            <div>
              <p className="text-xs font-semibold text-grape-400 uppercase tracking-widest mb-1">Dashboard</p>
              <h1 className="text-2xl sm:text-3xl font-bold text-white tracking-tight">
                Welcome back, <span className="text-grape-300">@{user}</span>
              </h1>
            </div>
            <button
              onClick={logout}
              className="inline-flex items-center gap-1.5 text-xs text-zinc-500 hover:text-zinc-300 transition-colors mt-1 flex-shrink-0"
            >
              <LogOut size={13} />
              Sign out
            </button>
          </div>

          {/* Stats */}
          <div className="grid grid-cols-2 sm:grid-cols-3 gap-3 mb-8">
            <StatCard
              label="Reviews this month"
              value={loading ? "—" : (stats?.reviews_this_month ?? 0)}
              sub={loading ? undefined : usageLabel}
            />
            <StatCard
              label="Total reviews"
              value={loading ? "—" : (stats?.total_reviews ?? 0)}
            />
            <StatCard
              label="Repos connected"
              value={loading ? "—" : (stats?.repos_count ?? 0)}
              sub="across your installs"
            />
          </div>

          {/* Recent reviews */}
          <div className="rounded-xl border border-zinc-800 bg-[#101015] overflow-hidden mb-6">
            <div className="flex items-center justify-between px-5 py-4 border-b border-zinc-800">
              <p className="text-sm font-semibold text-white">Recent reviews</p>
              <a
                href={GH_APP}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1.5 text-xs text-grape-400 hover:text-grape-300 transition-colors"
              >
                <Plus size={12} />
                Add repos
              </a>
            </div>

            {loading ? (
              <div className="flex flex-col divide-y divide-zinc-800">
                {[1, 2, 3].map(i => (
                  <div key={i} className="px-5 py-4 flex items-center gap-4">
                    <div className="h-3 w-48 rounded bg-zinc-800 animate-pulse" />
                    <div className="h-3 w-24 rounded bg-zinc-800 animate-pulse ml-auto" />
                  </div>
                ))}
              </div>
            ) : error ? (
              <div className="px-5 py-10 text-center">
                <p className="text-zinc-500 text-sm">Could not load reviews. Check your connection.</p>
              </div>
            ) : reviews.length === 0 ? (
              <div className="px-5 py-12 text-center flex flex-col items-center gap-4">
                <div className="w-10 h-10 rounded-full bg-grape-500/10 border border-grape-500/20 flex items-center justify-center">
                  <GitPullRequest size={18} className="text-grape-400" />
                </div>
                <div>
                  <p className="text-sm font-medium text-white mb-1">No reviews yet</p>
                  <p className="text-xs text-zinc-500">Open a pull request on a connected repo to get your first graph-proven review.</p>
                </div>
                <a
                  href={GH_APP}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-1.5 text-xs px-4 py-2 rounded-lg bg-grape-600 hover:bg-grape-500 text-white font-medium transition-all"
                >
                  Install on a repo
                </a>
              </div>
            ) : (
              <div className="divide-y divide-zinc-800">
                {reviews.map((r) => (
                  <div key={r.id} className="px-5 py-3.5 flex items-center gap-4">
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2 mb-0.5">
                        <a
                          href={r.pr_url}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="text-sm text-white hover:text-grape-300 transition-colors truncate"
                        >
                          {r.pr_title}
                        </a>
                        <ExternalLink size={11} className="text-zinc-600 flex-shrink-0" />
                      </div>
                      <p className="text-xs text-zinc-500">{r.repo}</p>
                    </div>
                    <div className="flex items-center gap-4 flex-shrink-0 text-right">
                      <span className="text-xs text-zinc-500 hidden sm:block">{r.findings} finding{r.findings !== 1 ? "s" : ""}</span>
                      <StatusBadge status={r.status} />
                      <span className="text-xs text-zinc-600 hidden sm:block">{timeAgo(r.created_at)}</span>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* Free tier nudge */}
          {stats && stats.monthly_limit !== null && stats.reviews_this_month >= stats.monthly_limit && (
            <div className="rounded-xl border border-grape-500/30 bg-grape-500/5 px-5 py-4 flex flex-col sm:flex-row sm:items-center gap-3 sm:gap-6">
              <div className="flex-1">
                <p className="text-sm font-semibold text-white mb-0.5">Monthly limit reached</p>
                <p className="text-xs text-zinc-400">Upgrade to Pro for unlimited reviews on public and private repos.</p>
              </div>
              <a
                href="/pricing"
                className="inline-flex items-center justify-center gap-1.5 text-xs px-4 py-2.5 rounded-lg bg-grape-600 hover:bg-grape-500 text-white font-medium transition-all flex-shrink-0"
              >
                See Pro plan
              </a>
            </div>
          )}

        </div>
      </main>
      <Footer />
    </>
  );
}
