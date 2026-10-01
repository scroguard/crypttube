import { NavLink } from "react-router";

export default function AdminNav() {
  const cls = ({ isActive }: { isActive: boolean }) =>
    `-mb-px border-b-2 px-4 py-2 text-sm font-medium ${isActive ? "border-accent text-white" : "border-transparent text-zinc-400 hover:text-white"}`;
  return (
    <nav className="mb-6 flex gap-1 border-b border-zinc-800" aria-label="Admin">
      <NavLink to="/admin/libraries" className={cls}>Libraries</NavLink>
      <NavLink to="/admin/jobs" className={cls}>Jobs</NavLink>
      <NavLink to="/admin/users" className={cls}>Users</NavLink>
      <NavLink to="/admin/import" className={cls}>Import</NavLink>
    </nav>
  );
}
