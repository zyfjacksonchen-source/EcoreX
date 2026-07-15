import * as Dialog from "@radix-ui/react-dialog";
import {
  Component,
  Suspense,
  useEffect,
  useRef,
  useState,
  type ErrorInfo,
  type ReactNode,
} from "react";

interface FeatureErrorBoundaryProps {
  active: boolean;
  children: ReactNode;
  label: string;
  onClose: () => void;
}

interface FeatureErrorBoundaryState {
  failed: boolean;
}

class FeatureErrorBoundary extends Component<
  FeatureErrorBoundaryProps,
  FeatureErrorBoundaryState
> {
  state: FeatureErrorBoundaryState = { failed: false };

  static getDerivedStateFromError(): FeatureErrorBoundaryState {
    return { failed: true };
  }

  componentDidCatch(_error: Error, _info: ErrorInfo): void {
    // The visible boundary is intentionally free of backend or asset details.
    // A page refresh resolves both transient network failures and an update that
    // replaced a content-addressed feature chunk while this page was open.
  }

  render() {
    if (!this.state.failed) return this.props.children;
    if (!this.props.active) return null;
    return (
      <FeatureLoadSurface
        label={this.props.label}
        state="error"
        onClose={this.props.onClose}
      />
    );
  }
}

interface FeatureLoadSurfaceProps {
  label: string;
  state: "loading" | "error";
  onClose: () => void;
}

function FeatureLoadSurface({ label, state, onClose }: FeatureLoadSurfaceProps) {
  const failed = state === "error";
  const closeButtonRef = useRef<HTMLButtonElement>(null);

  const handleOpenChange = (open: boolean) => {
    if (!open) onClose();
  };

  return (
    <Dialog.Root open modal onOpenChange={handleOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="ex-dialog-overlay" />
        <Dialog.Content
          className="ex-dialog ex-lazy-feature-surface"
          role={failed ? "alertdialog" : "dialog"}
          aria-busy={!failed}
          onOpenAutoFocus={(event) => {
            const button = closeButtonRef.current;
            if (!button) return;
            event.preventDefault();
            button.focus({ preventScroll: true });
          }}
          onCloseAutoFocus={(event) => {
            // App owns deterministic restoration because the trigger may have
            // moved or become hidden while this lazy feature was open.
            event.preventDefault();
          }}
        >
          <div className="ex-dialog-heading">
            <div>
              <Dialog.Title>
                {failed ? `${label}暂时无法打开` : `正在打开${label}`}
              </Dialog.Title>
              <Dialog.Description>
                {failed
                  ? "功能文件未能完整载入。刷新页面后可以继续当前任务。"
                  : "首次打开需要载入对应功能，任务内容不会受影响。"}
              </Dialog.Description>
            </div>
          </div>
          <div className="ex-dialog-actions">
            <Dialog.Close asChild>
              <button ref={closeButtonRef} className="ex-button" type="button">
                先关闭
              </button>
            </Dialog.Close>
            {failed ? (
              <button className="ex-button is-primary" type="button" onClick={() => window.location.reload()}>
                刷新页面
              </button>
            ) : null}
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

interface LazyFeatureBoundaryProps {
  active: boolean;
  children: ReactNode;
  label: string;
  onClose: () => void;
}

/**
 * Starts loading only when the feature is first opened, then keeps it mounted.
 * Keeping the subtree mounted preserves draft/dialog state across close and
 * reopen without paying its download/parse cost during the initial workspace.
 */
export function LazyFeatureBoundary({
  active,
  children,
  label,
  onClose,
}: LazyFeatureBoundaryProps) {
  const [openedOnce, setOpenedOnce] = useState(active);

  useEffect(() => {
    if (active) setOpenedOnce(true);
  }, [active]);

  if (!active && !openedOnce) return null;
  return (
    <FeatureErrorBoundary active={active} label={label} onClose={onClose}>
      <Suspense
        fallback={active ? <FeatureLoadSurface label={label} state="loading" onClose={onClose} /> : null}
      >
        {children}
      </Suspense>
    </FeatureErrorBoundary>
  );
}
