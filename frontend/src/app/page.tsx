"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { apiFetch } from "@/lib/api";
import { apiErrorStatus } from "@/lib/apiError";

export default function Home() {
  const router = useRouter();

  useEffect(() => {
    let alive = true;
    // Source de vérité d'auth = /me (cookie HttpOnly illisible en JS).
    // Seul un 401 signifie « non connecté ». Un 429, un 5xx ou une erreur réseau ne
    // disent rien de la session : on entre dans l'application, dont la coquille
    // (WorkspaceContext) gère l'attente et l'état transitoire.
    apiFetch("/api/v1/auth/me/")
      .then(() => alive && router.replace("/home"))
      .catch((e) => alive && router.replace(apiErrorStatus(e) === 401 ? "/login" : "/home"));
    return () => {
      alive = false;
    };
  }, [router]);

  return null;
}
