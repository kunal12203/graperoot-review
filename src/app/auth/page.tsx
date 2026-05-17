"use client";

import { useEffect, Suspense } from "react";
import { useSearchParams } from "next/navigation";

const DASH_URL = "https://graperoot-review-production.up.railway.app/dashboard";

function AuthHandler() {
  const params = useSearchParams();

  useEffect(() => {
    const user = params.get("user");
    if (user) {
      localStorage.setItem("gr_user", user);
    }
    window.location.replace(DASH_URL);
  }, [params]);

  return (
    <div className="min-h-screen flex items-center justify-center">
      <p className="text-zinc-500 text-sm">Redirecting to dashboard&hellip;</p>
    </div>
  );
}

export default function AuthPage() {
  return (
    <Suspense>
      <AuthHandler />
    </Suspense>
  );
}
