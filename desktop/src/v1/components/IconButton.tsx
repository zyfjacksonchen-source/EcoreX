import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from "react";
import * as Tooltip from "@radix-ui/react-tooltip";

interface IconButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  label: string;
  children: ReactNode;
  tooltipSide?: "top" | "right" | "bottom" | "left";
}

export const IconButton = forwardRef<HTMLButtonElement, IconButtonProps>(function IconButton({
    label,
    children,
    className = "",
    tooltipSide = "top",
    ...props
  }, ref) {
  const button = (
    <button
      {...props}
      ref={ref}
      type={props.type ?? "button"}
      className={`ex-icon-button ${className}`.trim()}
      aria-label={label}
      aria-hidden={props.disabled ? true : undefined}
      tabIndex={props.disabled ? -1 : props.tabIndex}
    >
      {children}
    </button>
  );
  return (
    <Tooltip.Root delayDuration={850}>
      <Tooltip.Trigger asChild>
        {props.disabled ? (
          <span
            className="ex-disabled-tooltip-trigger"
            tabIndex={0}
            role="button"
            aria-disabled="true"
            aria-label={label}
          >
            {button}
          </span>
        ) : button}
      </Tooltip.Trigger>
      <Tooltip.Portal>
        <Tooltip.Content className="ex-tooltip" side={tooltipSide} sideOffset={8}>
          {label}
          <Tooltip.Arrow className="ex-tooltip-arrow" />
        </Tooltip.Content>
      </Tooltip.Portal>
    </Tooltip.Root>
  );
});
