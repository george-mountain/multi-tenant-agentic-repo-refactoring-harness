import { Navigate, Route, Routes, useNavigate } from "react-router-dom";
import Logo from "./components/Logo";
import { useAuth } from "./lib/auth";
import DashboardPage from "./pages/DashboardPage";
import LoginPage from "./pages/LoginPage";
import RunDetailPage from "./pages/RunDetailPage";

export default function App() {
  const { session, signOut } = useAuth();
  const navigate = useNavigate();

  if (!session) return <LoginPage />;

  return (
    <>
      <header className="topbar">
        <div className="brand" onClick={() => navigate("/")}>
          <Logo />
          <span className="name">Refactory</span>
        </div>
        <div className="topbar-right">
          <span className="org-chip">
            <span className="dot" />
            {session.tenant_name}
          </span>
          <button className="btn btn-ghost btn-sm" onClick={signOut}>
            Sign out
          </button>
        </div>
      </header>
      <main className="container">
        <Routes>
          <Route path="/" element={<DashboardPage />} />
          <Route path="/runs/:runId" element={<RunDetailPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </>
  );
}
