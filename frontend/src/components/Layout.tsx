import { useState, type ReactNode } from "react";
import { Link, NavLink } from "react-router";
import { useLogout, useMe } from "../api/hooks";

const NAV = [
  ["/", "Home"],
  ["/scenes", "Scenes"],
  ["/galleries", "Galleries"],
  ["/images", "Images"],
  ["/performers", "Performers"],
  ["/studios", "Studios"],
  ["/tags", "Tags"],
] as const;

export default function Layout({ children }: { children: ReactNode }) {
  const { user, isAdmin, canUpload } = useMe();
  const logout = useLogout();
  const [menu, setMenu] = useState(false);

  const link = ({ isActive }: { isActive: boolean }) =>
    `whitespace-nowrap rounded-md px-3 py-1.5 text-sm font-medium ${isActive ? "bg-zinc-800 text-white" : "text-zinc-400 hover:text-white"}`;

  return (
    <div className="min-h-screen">
      <header className="sticky top-0 z-40 border-b border-zinc-800 bg-zinc-950/90 backdrop-blur">
        <div className="mx-auto flex h-14 max-w-[1920px] items-center gap-4 px-4">
          <Link to="/" className="flex items-center gap-2 font-bold tracking-tight">
            <img src="/favicon.svg" alt="" className="h-6 w-6" /> goon
          </Link>
          <nav className="flex min-w-0 flex-1 gap-1 overflow-x-auto" aria-label="Main">
            {NAV.map(([to, label]) => (
              <NavLink key={to} to={to} end={to === "/"} className={link}>{label}</NavLink>
            ))}
            {canUpload && <NavLink to="/uploads" className={link}>My uploads</NavLink>}
            {isAdmin && <NavLink to="/admin" className={link}>Admin</NavLink>}
          </nav>
          <div className="relative">
            <button className="btn-ghost" onClick={() => setMenu((m) => !m)} aria-haspopup="menu" aria-expanded={menu}>
              {user?.username} ▾
            </button>
            {menu && (
              <div role="menu" className="card absolute right-0 mt-2 w-44 bg-zinc-900 py-1 shadow-xl" onClick={() => setMenu(false)}>
                <Link role="menuitem" to="/account" className="block px-4 py-2 text-sm hover:bg-zinc-800">Account</Link>
                {isAdmin && <Link role="menuitem" to="/admin/users" className="block px-4 py-2 text-sm hover:bg-zinc-800">Users</Link>}
                <button role="menuitem" onClick={() => logout()} className="block w-full px-4 py-2 text-left text-sm hover:bg-zinc-800">Log out</button>
              </div>
            )}
          </div>
        </div>
      </header>
      <main className="mx-auto max-w-[1920px] px-4 py-5">{children}</main>
    </div>
  );
}
