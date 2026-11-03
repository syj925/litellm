"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import { CheckCircle2, CircleAlert } from "lucide-react";
import Link from "next/link";
import { FormEvent, useState } from "react";
import { Alert, AlertDescription, AlertTitle } from "@/components/shared/Alert";
import { PasswordInput } from "@/components/shared/PasswordInput";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { UiLoadingSpinner } from "@/components/ui/ui-loading-spinner";
import { uiHref } from "@/utils/uiHref";
import { RetailConfig, retailKeys, retailRequest } from "./api";
import PublicSiteShell from "./public-site/PublicSiteShell";
import styles from "./public-site/PublicAuth.module.css";

export function RegistrationLink() {
  const config = useQuery({
    queryKey: retailKeys.config,
    queryFn: () => retailRequest<RetailConfig>("/config", null),
    retry: false,
  });
  if (!config.data?.registration_enabled) return null;
  return (
    <p className="text-center text-sm text-muted-foreground">
      New here?{" "}
      <Link href={uiHref("signup")} className="font-medium text-foreground underline-offset-4 hover:underline">
        Create an account
      </Link>
    </p>
  );
}

export default function SignupPanel() {
  const config = useQuery({
    queryKey: retailKeys.config,
    queryFn: () => retailRequest<RetailConfig>("/config", null),
    retry: false,
  });
  const [values, setValues] = useState({ display_name: "", email: "", password: "", confirm: "" });
  const [validation, setValidation] = useState<string | null>(null);
  const registration = useMutation({
    mutationFn: () =>
      retailRequest<{ user_id: string; email: string; login_url: string }>("/signup", null, {
        method: "POST",
        body: JSON.stringify({
          email: values.email.trim(),
          password: values.password,
          display_name: values.display_name.trim(),
        }),
      }),
    onSuccess: () => setValues((current) => ({ ...current, password: "", confirm: "" })),
  });

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (values.password.length < 12 || values.password.length > 128) {
      setValidation("Use a password with 12 to 128 characters.");
      return;
    }
    if (values.password !== values.confirm) {
      setValidation("Your passwords do not match.");
      return;
    }
    setValidation(null);
    registration.mutate();
  };

  return (
    <PublicSiteShell activePage="signup">
      <main className={styles.viewport}>
        <Card className={styles.card}>
          <CardContent className={styles.content}>
            <div className={styles.formStack}>
              <div className={styles.heading}>
                <h1 className={styles.title}>Create your account</h1>
                <p className={styles.description}>One account for your models, API keys, and usage.</p>
              </div>
              {registration.isSuccess ? (
                <div className="flex flex-col gap-5">
                  <Alert variant="success">
                    <CheckCircle2 />
                    <AlertTitle>Your account is ready</AlertTitle>
                    <AlertDescription>
                      Sign in with your email and password. Your wallet starts at $0.00; contact your administrator to
                      add funds.
                    </AlertDescription>
                  </Alert>
                  <Link href={uiHref("login")} className={styles.primaryLink}>
                    Continue to login
                  </Link>
                </div>
              ) : config.isPending ? (
                <div
                  className="flex items-center justify-center gap-2 py-8 text-sm text-muted-foreground"
                  role="status"
                >
                  <UiLoadingSpinner className="size-4" />
                  Loading registration...
                </div>
              ) : config.error ? (
                <Alert variant="error">
                  <CircleAlert />
                  <AlertTitle>Registration is unavailable</AlertTitle>
                  <AlertDescription>
                    {config.error.message}
                    <Button variant="link" className="mt-2 block h-auto px-0" onClick={() => void config.refetch()}>
                      Try again
                    </Button>
                  </AlertDescription>
                </Alert>
              ) : !config.data?.registration_enabled ? (
                <Alert variant="info">
                  <CircleAlert />
                  <AlertTitle>Registration is currently closed</AlertTitle>
                  <AlertDescription>Contact your administrator to request an account.</AlertDescription>
                </Alert>
              ) : (
                <form onSubmit={submit} className="flex flex-col gap-5">
                  <FieldGroup>
                    <Field>
                      <FieldLabel htmlFor="signup-name">Name (optional)</FieldLabel>
                      <Input
                        id="signup-name"
                        autoComplete="name"
                        value={values.display_name}
                        onChange={(event) => setValues({ ...values, display_name: event.target.value })}
                        maxLength={80}
                        disabled={registration.isPending}
                        className="h-10"
                        placeholder="Your name"
                      />
                    </Field>
                    <Field>
                      <FieldLabel htmlFor="signup-email">Email</FieldLabel>
                      <Input
                        id="signup-email"
                        type="email"
                        autoComplete="email"
                        value={values.email}
                        onChange={(event) => setValues({ ...values, email: event.target.value })}
                        required
                        maxLength={254}
                        disabled={registration.isPending}
                        className="h-10"
                        placeholder="you@example.com"
                      />
                    </Field>
                    <Field>
                      <FieldLabel htmlFor="signup-password">Password</FieldLabel>
                      <PasswordInput
                        id="signup-password"
                        autoComplete="new-password"
                        value={values.password}
                        onChange={(event) => setValues({ ...values, password: event.target.value })}
                        required
                        minLength={12}
                        maxLength={128}
                        disabled={registration.isPending}
                        groupClassName="h-10"
                        placeholder="At least 12 characters"
                      />
                    </Field>
                    <Field>
                      <FieldLabel htmlFor="signup-confirm">Confirm password</FieldLabel>
                      <PasswordInput
                        id="signup-confirm"
                        autoComplete="new-password"
                        value={values.confirm}
                        onChange={(event) => setValues({ ...values, confirm: event.target.value })}
                        required
                        minLength={12}
                        maxLength={128}
                        disabled={registration.isPending}
                        groupClassName="h-10"
                        placeholder="Enter your password again"
                      />
                    </Field>
                  </FieldGroup>
                  {(validation || registration.error) && (
                    <Alert variant="error">
                      <CircleAlert />
                      <AlertTitle>Unable to create your account</AlertTitle>
                      <AlertDescription>{validation || registration.error?.message}</AlertDescription>
                    </Alert>
                  )}
                  <Button type="submit" size="lg" disabled={registration.isPending} className="w-full">
                    {registration.isPending && <UiLoadingSpinner className="size-4" />}
                    {registration.isPending ? "Creating account..." : "Create account"}
                  </Button>
                  <p className="text-center text-xs leading-relaxed text-muted-foreground">
                    New accounts start with a $0.00 balance. Add funds through your administrator before making API
                    requests.
                  </p>
                </form>
              )}
              {!registration.isSuccess && (
                <p className="text-center text-sm text-muted-foreground">
                  Already have an account?{" "}
                  <Link
                    href={uiHref("login")}
                    className="font-medium text-foreground underline-offset-4 hover:underline"
                  >
                    Log in
                  </Link>
                </p>
              )}
            </div>
          </CardContent>
        </Card>
      </main>
    </PublicSiteShell>
  );
}
