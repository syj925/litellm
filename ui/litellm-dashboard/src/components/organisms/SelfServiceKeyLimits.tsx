import { Input } from "@/components/ui/input";
import { MountedFormField } from "../common_components/MountedFormField";
import BudgetDurationDropdown from "../common_components/budget_duration_dropdown";
import NumericalInput from "../shared/numerical_input";
import { selfServiceKeyFields } from "./keyFormAccess";

export default function SelfServiceKeyLimits() {
  return (
    <section className="mb-8 space-y-4" aria-label="Key limits and expiry">
      <h3 className="text-lg font-medium">Limits and expiry</h3>
      <p className="text-sm text-muted-foreground">
        These limits apply in addition to your account permissions and wallet balance.
      </p>
      {selfServiceKeyFields.map(({ name, label, step }) => (
        <MountedFormField key={name} name={name} label={label}>
          {({ value, ...control }) => (
            <NumericalInput {...control} value={value as number | string | undefined} min={0} step={step} />
          )}
        </MountedFormField>
      ))}
      <MountedFormField name="budget_duration" label="Reset Budget">
        {({ value, id, onChange }) => (
          <BudgetDurationDropdown
            id={id}
            value={value as string | null | undefined}
            onChange={onChange}
            showNeverResets
          />
        )}
      </MountedFormField>
      <MountedFormField name="duration" label="Expires after" help="For example, 30d. Leave empty for no expiry.">
        {({ value, ...control }) => (
          <Input {...control} value={(value as string | undefined) ?? ""} placeholder="30d" />
        )}
      </MountedFormField>
    </section>
  );
}
