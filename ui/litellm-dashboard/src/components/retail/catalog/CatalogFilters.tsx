import { Checkbox } from "@/components/ui/checkbox";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { capabilityOptions, type CatalogFilters as Filters } from "./catalogState";
import { getModelIdentity } from "./modelIdentity";
import type { CatalogModel } from "./types";

export function CatalogSelect({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string;
  options: { value: string; label: string }[];
  onChange: (value: string) => void;
}) {
  return (
    <Select
      value={value}
      items={options}
      onValueChange={(next) => {
        if (next != null) onChange(next);
      }}
    >
      <SelectTrigger aria-label={label} className="h-9 w-full text-xs">
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        {options.map((option) => (
          <SelectItem key={option.value} value={option.value}>
            {option.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

function CheckOption({
  id,
  label,
  checked,
  onChange,
  count,
}: {
  id: string;
  label: string;
  checked: boolean;
  onChange: () => void;
  count?: number;
}) {
  return (
    <label
      htmlFor={id}
      className="flex cursor-pointer items-center gap-2.5 py-1 text-sm text-muted-foreground hover:text-foreground"
    >
      <Checkbox id={id} checked={checked} onCheckedChange={onChange} />
      <span className="min-w-0 flex-1 truncate">{label}</span>
      {count != null && <span className="text-xs tabular-nums">{count}</span>}
    </label>
  );
}

function toggle(values: string[], value: string): string[] {
  return values.includes(value) ? values.filter((item) => item !== value) : [...values, value];
}

export default function CatalogFilters({
  models,
  filters,
  onChange,
  prefix,
}: {
  models: CatalogModel[];
  filters: Filters;
  onChange: (value: Partial<Filters>) => void;
  prefix: string;
}) {
  const authors = [...new Set(models.map((model) => getModelIdentity(model.id, model.provider).author))].sort();
  const modalities = [...new Set(models.flatMap((model) => model.input_modalities))].sort();
  const pools = [
    ...new Map(
      models.flatMap((model) => model.offers.map((offer) => [offer.pool_id, offer.pool_name] as const)),
    ).entries(),
  ];
  return (
    <div className="space-y-6 text-sm">
      <fieldset className="space-y-2">
        <legend className="mb-2 font-medium">Model authors</legend>
        {authors.map((author) => (
          <CheckOption
            key={author}
            id={`${prefix}-author-${author}`}
            label={author}
            checked={filters.authors.includes(author)}
            onChange={() => onChange({ authors: toggle(filters.authors, author) })}
            count={models.filter((model) => getModelIdentity(model.id, model.provider).author === author).length}
          />
        ))}
      </fieldset>
      {modalities.length > 0 && (
        <fieldset className="space-y-2">
          <legend className="mb-2 font-medium">Input modalities</legend>
          {modalities.map((modality) => (
            <CheckOption
              key={modality}
              id={`${prefix}-input-${modality}`}
              label={modality.charAt(0).toUpperCase() + modality.slice(1)}
              checked={filters.input.includes(modality)}
              onChange={() => onChange({ input: toggle(filters.input, modality) })}
            />
          ))}
        </fieldset>
      )}
      <fieldset className="space-y-2">
        <legend className="mb-2 font-medium">Capabilities</legend>
        {capabilityOptions.map(({ id, label }) => (
          <CheckOption
            key={id}
            id={`${prefix}-capability-${id}`}
            label={label}
            checked={filters.capabilities.includes(id)}
            onChange={() =>
              onChange({
                capabilities: filters.capabilities.includes(id)
                  ? filters.capabilities.filter((item) => item !== id)
                  : [...filters.capabilities, id],
              })
            }
            count={models.filter((model) => model.capabilities[id] === true).length}
          />
        ))}
      </fieldset>
      <div className="space-y-2">
        <p className="font-medium">Context length</p>
        <CatalogSelect
          label="Minimum context length"
          value={String(filters.context)}
          onChange={(value) => onChange({ context: Number(value) })}
          options={[
            { value: "0", label: "Any context" },
            ...[32_000, 128_000, 200_000, 1_000_000].map((value) => ({
              value: String(value),
              label: value >= 1_000_000 ? "1M+ tokens" : `${value / 1000}K+ tokens`,
            })),
          ]}
        />
      </div>
      <div className="space-y-2">
        <p className="font-medium">Input price</p>
        <CatalogSelect
          label="Maximum input price"
          value={String(filters.max_price)}
          onChange={(value) => onChange({ max_price: Number(value) })}
          options={[
            { value: "-1", label: "Any price" },
            ...[0.1, 1, 5, 10].map((value) => ({ value: String(value), label: `Up to $${value} / M` })),
          ]}
        />
      </div>
      <fieldset className="space-y-2">
        <legend className="mb-2 font-medium">Resource pools</legend>
        {pools.map(([id, name]) => (
          <CheckOption
            key={id}
            id={`${prefix}-pool-${id}`}
            label={name}
            checked={filters.pools.includes(id)}
            onChange={() => onChange({ pools: toggle(filters.pools, id) })}
          />
        ))}
      </fieldset>
    </div>
  );
}
