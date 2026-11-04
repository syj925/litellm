export default function MonthlyBudgetNote() {
  return (
    <div className="space-y-2 text-sm leading-relaxed text-muted-foreground">
      <p>
        Calendar-month limits apply across this account’s keys and resource pools. Wallet funds and model access still
        apply.
      </p>
      <details>
        <summary className="w-fit cursor-pointer font-medium text-foreground">How monthly plans work</summary>
        <div className="mt-3 max-w-prose space-y-2">
          <p>This month is frozen. Changes apply to the next full month; unused capacity does not carry over.</p>
          <p>
            Accepted requests settle against their original month. If a required monthly window is not ready, new
            requests are blocked until it is created.
          </p>
        </div>
      </details>
    </div>
  );
}
