"use client";

import { useEffect, Suspense } from "react";
import { useSearchParams } from "next/navigation";

function AuthHandler() {
  const params = useSearchParams();

  useEffect(() => {
    const user  = params.get("user");
    const token = params.get("token");
    if (user)  localStorage.setItem("gr_user",  user);
    if (token) localStorage.setItem("gr_token", token);
    window.location.replace("/dashboard");
  }, [params]);

  return (
    <div className="min-h-screen flex items-center justify-center">
      <p className="text-zinc-500 text-sm">Signing you in&hellip;</p>
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
