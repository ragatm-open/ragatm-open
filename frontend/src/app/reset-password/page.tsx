// frontend/src/app/reset-password/page.tsx
import { Suspense } from "react";
import { getTranslations } from "next-intl/server";
import ResetPasswordClient from "./ResetPasswordClient";

export async function generateMetadata() {
  const t = await getTranslations("passwordReset");
  return { title: t("title") };
}

export default async function ResetPasswordPage() {
  const tc = await getTranslations("common");
  return (
    <Suspense fallback={<div style={{ padding: 16 }}>{tc("loading")}</div>}>
      <ResetPasswordClient />
    </Suspense>
  );
}
