import { AdminGate } from "../_components/AdminGate";

export default function ClaimsLayout({
  children,
}: {
  children: React.ReactNode
}) {
  return <AdminGate>{children}</AdminGate>
}
