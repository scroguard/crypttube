import { lazy, Suspense } from "react";
import { Navigate, Route, Routes } from "react-router";
import { useAuthStatus, useMe } from "./api/hooks";
import Layout from "./components/Layout";
import { ErrorBox, Spinner } from "./components/ui";
import Login from "./pages/Login";
import Setup from "./pages/Setup";

const Home = lazy(() => import("./pages/Home"));
const Scenes = lazy(() => import("./pages/Scenes"));
const SceneDetail = lazy(() => import("./pages/SceneDetail"));
const Galleries = lazy(() => import("./pages/Galleries"));
const GalleryDetail = lazy(() => import("./pages/GalleryDetail"));
const Images = lazy(() => import("./pages/Images"));
const Performers = lazy(() => import("./pages/Performers"));
const PerformerDetail = lazy(() => import("./pages/PerformerDetail"));
const Studios = lazy(() => import("./pages/Studios"));
const StudioDetail = lazy(() => import("./pages/StudioDetail"));
const Tags = lazy(() => import("./pages/Tags"));
const TagDetail = lazy(() => import("./pages/TagDetail"));
const Account = lazy(() => import("./pages/Account"));
const Uploads = lazy(() => import("./pages/Uploads"));
const UploadLibrary = lazy(() => import("./pages/UploadLibrary"));
const AdminLibraries = lazy(() => import("./pages/admin/Libraries"));
const AdminJobs = lazy(() => import("./pages/admin/Jobs"));
const AdminUsers = lazy(() => import("./pages/admin/Users"));
const AdminUserEdit = lazy(() => import("./pages/admin/UserEdit"));
const AdminStashImport = lazy(() => import("./pages/admin/StashImport"));

export default function App() {
  const status = useAuthStatus();
  if (status.isPending) return <Spinner className="mt-40" />;
  if (status.isError) return <ErrorBox error={status.error} className="m-8" />;
  if (status.data.setup_required) return <Setup />;
  if (!status.data.user) return <Login />;
  return (
    <Layout>
      <Suspense fallback={<Spinner className="mt-24" />}>
        <Routes>
          <Route path="/" element={<Home />} />
          <Route path="/scenes" element={<Scenes />} />
          <Route path="/scenes/:id" element={<SceneDetail />} />
          <Route path="/galleries" element={<Galleries />} />
          <Route path="/galleries/:id" element={<GalleryDetail />} />
          <Route path="/images" element={<Images />} />
          <Route path="/performers" element={<Performers />} />
          <Route path="/performers/:id" element={<PerformerDetail />} />
          <Route path="/studios" element={<Studios />} />
          <Route path="/studios/:id" element={<StudioDetail />} />
          <Route path="/tags" element={<Tags />} />
          <Route path="/tags/:id" element={<TagDetail />} />
          <Route path="/account" element={<Account />} />
          <Route path="/uploads" element={<Uploads />} />
          <Route path="/uploads/:id" element={<UploadLibrary />} />
          <Route path="/admin/*" element={<AdminRoutes />} />
          <Route path="*" element={<p className="p-8 text-zinc-400">Page not found.</p>} />
        </Routes>
      </Suspense>
    </Layout>
  );
}

function AdminRoutes() {
  const { isAdmin } = useMe();
  if (!isAdmin) return <p className="p-8 text-zinc-400">Admins only.</p>;
  return (
    <Routes>
      <Route index element={<Navigate to="libraries" replace />} />
      <Route path="libraries" element={<AdminLibraries />} />
      <Route path="jobs" element={<AdminJobs />} />
      <Route path="users" element={<AdminUsers />} />
      <Route path="users/:id" element={<AdminUserEdit />} />
      <Route path="import" element={<AdminStashImport />} />
    </Routes>
  );
}
