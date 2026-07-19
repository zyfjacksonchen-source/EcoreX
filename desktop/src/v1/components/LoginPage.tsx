import { useState, type FormEvent } from "react";

interface LoginPageProps {
  busy: boolean;
  error: string | null;
  onClearError: () => void;
  onLogin: (identifier: string, password: string) => Promise<unknown>;
}

export function LoginPage({
  busy,
  error,
  onClearError,
  onLogin,
}: LoginPageProps) {
  const [identifier, setIdentifier] = useState("");
  const [password, setPassword] = useState("");

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const normalizedIdentifier = identifier.trim();
    if (!normalizedIdentifier || !password || busy) return;
    const receipt = await onLogin(normalizedIdentifier, password);
    if (receipt) setPassword("");
  };

  return (
    <main className="ex-login-page">
      <section className="ex-login-panel" aria-labelledby="ex-login-title">
        <div className="ex-login-brand">
          <span className="ex-brand-mark" aria-hidden="true">E</span>
          <h1 id="ex-login-title">EcoreX</h1>
        </div>
        <form className="ex-login-form" onSubmit={(event) => void submit(event)}>
          <label className="ex-field" htmlFor="ex-login-identifier">
            <span>账号或邮箱</span>
            <input
              id="ex-login-identifier"
              name="identifier"
              type="text"
              autoComplete="username"
              autoCapitalize="none"
              spellCheck={false}
              value={identifier}
              disabled={busy}
              onChange={(event) => {
                setIdentifier(event.target.value);
                if (error) onClearError();
              }}
            />
          </label>
          <label className="ex-field" htmlFor="ex-login-password">
            <span>密码</span>
            <input
              id="ex-login-password"
              name="password"
              type="password"
              autoComplete="current-password"
              value={password}
              disabled={busy}
              onChange={(event) => {
                setPassword(event.target.value);
                if (error) onClearError();
              }}
            />
          </label>
          {error ? <p className="ex-login-error" role="alert">{error}</p> : null}
          <button
            className="ex-button is-primary ex-login-submit"
            type="submit"
            disabled={busy || !identifier.trim() || !password}
            aria-busy={busy}
          >
            {busy ? "正在进入 EcoreX" : "登录"}
          </button>
        </form>
      </section>
    </main>
  );
}
