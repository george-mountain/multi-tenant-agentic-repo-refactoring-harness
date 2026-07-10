import { FormEvent, useState } from "react";
import Logo from "../components/Logo";
import { api, ApiError } from "../lib/api";
import { useAuth } from "../lib/auth";

const FEATURES = [
  {
    icon: "🔍",
    title: "Autonomous code audit",
    desc: "The agent inspects your codebase and finds what needs refactoring — no instructions required.",
  },
  {
    icon: "🛡️",
    title: "Verified, sandboxed changes",
    desc: "Every edit runs in an isolated sandbox and must pass your tests, linters, and a reviewer agent.",
  },
  {
    icon: "🔀",
    title: "Ready-to-review pull requests",
    desc: "Clean, step-by-step commits on a dedicated branch. You stay in control of the merge.",
  },
];

export default function LoginPage() {
  const { signIn } = useAuth();
  const [mode, setMode] = useState<"login" | "register">("login");
  const [tenantName, setTenantName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const session =
        mode === "login"
          ? await api.login(email, password)
          : await api.register(tenantName, email, password);
      signIn(session);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Something went wrong. Please try again.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="auth-layout">
      <aside className="auth-brand">
        <div className="brand">
          <Logo size={34} />
          <span className="name" style={{ fontSize: 18 }}>
            Refactory
          </span>
        </div>
        <h1>
          Paste a repo URL.
          <br />
          Get a <em>refactoring PR</em> back.
        </h1>
        <p className="tagline">
          An autonomous AI engineer that audits your codebase, plans the work, refactors it in a
          sandbox, and opens a verified pull request.
        </p>
        <ul className="feature-list">
          {FEATURES.map((feature) => (
            <li key={feature.title}>
              <span className="feature-icon">{feature.icon}</span>
              <div>
                <b>{feature.title}</b>
                <span className="desc">{feature.desc}</span>
              </div>
            </li>
          ))}
        </ul>
      </aside>

      <section className="auth-form-side">
        <div className="auth-card">
          <h2>{mode === "login" ? "Welcome back" : "Create your workspace"}</h2>
          <p className="sub">
            {mode === "login"
              ? "Sign in to your organization's workspace."
              : "Set up an organization to start running refactors."}
          </p>
          <form onSubmit={submit}>
            {mode === "register" && (
              <div className="field">
                <label htmlFor="org">Organization name</label>
                <input
                  id="org"
                  className="input"
                  value={tenantName}
                  onChange={(e) => setTenantName(e.target.value)}
                  placeholder="Acme Inc."
                  required
                  minLength={2}
                />
              </div>
            )}
            <div className="field">
              <label htmlFor="email">Email</label>
              <input
                id="email"
                className="input"
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="you@company.com"
                required
              />
            </div>
            <div className="field">
              <label htmlFor="password">Password</label>
              <input
                id="password"
                className="input"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder={mode === "register" ? "At least 8 characters" : "••••••••"}
                required
                minLength={8}
              />
            </div>
            {error && <div className="form-error">⚠ {error}</div>}
            <button className="btn btn-primary" style={{ width: "100%" }} type="submit" disabled={busy}>
              {busy ? "One moment…" : mode === "login" ? "Sign in" : "Create workspace"}
            </button>
          </form>
          <p className="auth-switch">
            {mode === "login" ? (
              <>
                New here?{" "}
                <button onClick={() => { setMode("register"); setError(""); }}>Create a workspace</button>
              </>
            ) : (
              <>
                Already have an account?{" "}
                <button onClick={() => { setMode("login"); setError(""); }}>Sign in</button>
              </>
            )}
          </p>
        </div>
      </section>
    </div>
  );
}
