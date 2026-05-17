"use client";

import { useEffect, useState, useCallback } from "react";
import { useRouter } from "next/navigation";
import Navbar from "@/components/Navbar";
import Footer from "@/components/Footer";
import {
  GitPullRequest, CheckCircle, AlertCircle, Clock,
  ExternalLink, LogOut, Plus, Loader2, Sparkles, RefreshCw,
} from "lucide-react";

const API     = "https://graperoot-review-production.up.railway.app";
const GH_APP  = "https://github.com/apps/graperoot-review/installations/new/permissions?target_id=84341876";

interface Stats {
  reviews_this_month: number;
  monthly_limit: number | null;
  total_reviews: number;
  repos_count: number;
}

interface PR {
  number: number;
  title: string;
  repo: string;
  pr_url: string;
  author: string;
  branch: string;
  updated_at: string;
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

function timeAgo(iso: string) {
  const diff = Date.now() - new Date(iso).getTime();
  const m = Math.floor(diff / 60000);
  if (m < 60)  return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24)  return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
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

function PRCard({
  pr, token, onQueued,
}: {
  pr: PR;
  token: string;
  onQueued: (repo: string, num: number) => void;
}) {
  const [state, setState] = useState<"idle" | "loading" | "queued" | "error">("idle");
  const [errMsg, setErrMsg] = useState("");

  async function requestReview() {
    setState("loading");
    setErrMsg("");
    try {
      const res = await fetch(`${API}/api/review`, {
        method: "POST",
        headers: { "Authorization": `Bearer ${token}`, "Content-Type": "application/json" },
        body: JSON.stringify({ pr_url: pr.pr_url }),
      });
      const data = await res.json();
      if (!res.ok) {
        setErrMsg(data.message || data.error || "Failed");
        setState("error");
      } else {
        setState("queued");
        onQueued(pr.repo, pr.number);
      }
    } catch {
      setErrMsg("Network error");
      setState("error");
    }
  }

  return (
    <div className="px-5 py-4 flex items-start gap-4 border-b border-zinc-800 last:border-0">
      <div className="w-8 h-8 rounded-lg bg-grape-500/10 border border-grape-500/20 flex items-center justify-center flex-shrink-0 mt-0.5">
        <GitPullRequest size={14} className="text-grape-400" />
      </div>

      <div className="min-w-0 flex-1">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <a
              href={pr.pr_url}
              target="_blank"
              rel="noopener noreferrer"
              className="text-sm font-medium text-white hover:text-grape-300 transition-colors line-clamp-1"
            >
              {pr.title}
            </a>
            <p className="text-xs text-zinc-500 mt-0.5">
              <span className="text-zinc-400">{pr.repo}</span>
              {" "}·{" "}#{pr.number}
              {" "}·{" "}by <span className="text-zinc-400">@{pr.author}</span>
              {" "}·{" "}{pr.branch}
              {" "}·{" "}{timeAgo(pr.updated_at)}
            </p>
          </div>

          <div className="flex-shrink-0">
            {state === "queued" ? (
              <span className="inline-flex items-center gap-1.5 text-xs text-emerald-400 font-medium">
                <CheckCircle size={12} /> Queued
              </span>
            ) : (
              <button
                onClick={requestReview}
                disabled={state === "loading"}
                className="inline-flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-lg bg-grape-600 hover:bg-grape-500 disabled:opacity-50 disabled:cursor-not-allowed text-white font-medium transition-all"
              >
                {state === "loading" ? (
                  <><Loader2 size={11} className="animate-spin" /> Queuing…</>
                ) : (
                  <><Sparkles size={11} /> Request Review</>
                )}
              </button>
            )}
          </div>
        </div>
        {state === "error" && (
          <p className="text-xs text-red-400 mt-1">{errMsg}</p>
        )}
      </div>
    </div>
  );
}

export default function DashboardPage() {
  const router  = useRouter();
  const [user,    setUser]    = useState<string | null>(null);
  const [token,   setToken]   = useState("");
  const [stats,   setStats]   = useState<Stats | null>(null);
  const [prs,     setPrs]     = useState<PR[]>([]);
  const [reviews, setReviews] = useState<Review[]>([]);
  const [loadingStats,   setLoadingStats]   = useState(true);
  const [loadingPRs,     setLoadingPRs]     = useState(true);
  const [loadingReviews, setLoadingReviews] = useState(true);
  const [queuedPRs, setQueuedPRs] = useState<Set<string>>(new Set());

  const fetchData = useCallback((tok: string) => {
    const headers: HeadersInit = { Authorization: `Bearer ${tok}` };

    fetch(`${API}/api/stats`, { headers })
      .then(r => r.ok ? r.json() : null)
      .then(s => { if (s) setStats(s); })
      .finally(() => setLoadingStats(false));

    fetch(`${API}/api/prs`, { headers })
      .then(r => r.ok ? r.json() : [])
      .then(p => { if (Array.isArray(p)) setPrs(p); })
      .finally(() => setLoadingPRs(false));

    fetch(`${API}/api/reviews`, { headers })
      .then(r => r.ok ? r.json() : [])
      .then(r => { if (Array.isArray(r)) setReviews(r); })
      .finally(() => setLoadingReviews(false));
  }, []);

  useEffect(() => {
    const storedUser  = localStorage.getItem("gr_user");
    const storedToken = localStorage.getItem("gr_token") || "";
    if (!storedUser) { router.replace("/login"); return; }
    setUser(storedUser);
    setToken(storedToken);
    fetchData(storedToken);
  }, [router, fetchData]);

  function logout() {
    localStorage.removeItem("gr_user");
    localStorage.removeItem("gr_token");
    router.push("/");
  }

  function handleQueued(repo: string, num: number) {
    setQueuedPRs(prev => new Set(prev).add(`${repo}#${num}`));
  }

  if (!user) return null;

  const usageSub = stats
    ? stats.monthly_limit
      ? `${stats.reviews_this_month} of ${stats.monthly_limit} used this month`
      : "Unlimited (Pro)"
    : undefined;

  // PRs that already have a review in progress
  const reviewedPRUrls = new Set(reviews.map(r => r.pr_url));

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
            <div className="flex items-center gap-3 mt-1">
              <button
                onClick={() => { setLoadingPRs(true); setLoadingReviews(true); fetchData(token); }}
                className="text-zinc-500 hover:text-zinc-300 transition-colors"
                title="Refresh"
              >
                <RefreshCw size={14} />
              </button>
              <button
                onClick={logout}
                className="inline-flex items-center gap-1.5 text-xs text-zinc-500 hover:text-zinc-300 transition-colors"
              >
                <LogOut size={13} /> Sign out
              </button>
            </div>
          </div>

          {/* Stats */}
          <div className="grid grid-cols-2 sm:grid-cols-3 gap-3 mb-8">
            <StatCard label="Reviews this month" value={loadingStats ? "—" : (stats?.reviews_this_month ?? 0)} sub={loadingStats ? undefined : usageSub} />
            <StatCard label="Total reviews" value={loadingStats ? "—" : (stats?.total_reviews ?? 0)} />
            <StatCard label="Repos connected" value={loadingStats ? "—" : (stats?.repos_count ?? 0)} sub="across your installs" />
          </div>

          {/* Open PRs */}
          <div className="rounded-xl border border-zinc-800 bg-[#101015] overflow-hidden mb-6">
            <div className="flex items-center justify-between px-5 py-4 border-b border-zinc-800">
              <div>
                <p className="text-sm font-semibold text-white">Open pull requests</p>
                <p className="text-xs text-zinc-500 mt-0.5">Across all repos where the App is installed</p>
              </div>
              <a
                href={GH_APP}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1.5 text-xs text-grape-400 hover:text-grape-300 transition-colors flex-shrink-0"
              >
                <Plus size={12} /> Add repos
              </a>
            </div>

            {loadingPRs ? (
              <div className="divide-y divide-zinc-800">
                {[1, 2, 3].map(i => (
                  <div key={i} className="px-5 py-4 flex items-center gap-3">
                    <div className="w-8 h-8 rounded-lg bg-zinc-800 animate-pulse flex-shrink-0" />
                    <div className="flex-1 flex flex-col gap-2">
                      <div className="h-3 w-64 rounded bg-zinc-800 animate-pulse" />
                      <div className="h-2.5 w-40 rounded bg-zinc-800 animate-pulse" />
                    </div>
                  </div>
                ))}
              </div>
            ) : prs.length === 0 ? (
              <div className="px-5 py-12 text-center flex flex-col items-center gap-4">
                <div className="w-10 h-10 rounded-full bg-grape-500/10 border border-grape-500/20 flex items-center justify-center">
                  <GitPullRequest size={18} className="text-grape-400" />
                </div>
                <div>
                  <p className="text-sm font-medium text-white mb-1">No open pull requests</p>
                  <p className="text-xs text-zinc-500">Open a PR on a connected repo, or install the App on more repos.</p>
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
              <div>
                {prs.map(pr => (
                  <PRCard
                    key={`${pr.repo}#${pr.number}`}
                    pr={pr}
                    token={token}
                    onQueued={handleQueued}
                  />
                ))}
              </div>
            )}
          </div>

          {/* Recent Reviews */}
          <div className="rounded-xl border border-zinc-800 bg-[#101015] overflow-hidden mb-6">
            <div className="px-5 py-4 border-b border-zinc-800">
              <p className="text-sm font-semibold text-white">Recent reviews</p>
            </div>

            {loadingReviews ? (
              <div className="divide-y divide-zinc-800">
                {[1, 2, 3].map(i => (
                  <div key={i} className="px-5 py-4 flex gap-4">
                    <div className="h-3 w-48 rounded bg-zinc-800 animate-pulse" />
                    <div className="h-3 w-20 rounded bg-zinc-800 animate-pulse ml-auto" />
                  </div>
                ))}
              </div>
            ) : reviews.length === 0 ? (
              <div className="px-5 py-10 text-center">
                <p className="text-zinc-500 text-sm">No reviews yet — click "Request Review" on any open PR above.</p>
              </div>
            ) : (
              <div className="divide-y divide-zinc-800">
                {reviews.map(r => (
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
                    <div className="flex items-center gap-4 flex-shrink-0">
                      <span className="text-xs text-zinc-500 hidden sm:block">{r.findings} finding{r.findings !== 1 ? "s" : ""}</span>
                      {r.status === "completed"
                        ? <span className="inline-flex items-center gap-1 text-xs text-emerald-400"><CheckCircle size={11} /> Done</span>
                        : r.status === "pending"
                        ? <span className="inline-flex items-center gap-1 text-xs text-amber-400"><Clock size={11} /> Pending</span>
                        : <span className="inline-flex items-center gap-1 text-xs text-red-400"><AlertCircle size={11} /> Failed</span>
                      }
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
