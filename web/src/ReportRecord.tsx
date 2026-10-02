export function ReportRecord({ label, value }: { label: string; value: string }) {
  return (
    <div className="record-row">
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}
