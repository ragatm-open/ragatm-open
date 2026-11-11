"use client";

import React, { useState } from "react";
import { Icon } from "./Icon";

export function Field({
  label,
  type = "text",
  value,
  onChange,
  placeholder,
  icon,
  hint,
  error,
  right,
  autoFocus,
  name,
  required,
  id,
  ...rest
}: {
  label?: string;
  type?: string;
  value?: string;
  onChange?: (v: string) => void;
  placeholder?: string;
  icon?: string;
  hint?: string;
  error?: string | null;
  right?: React.ReactNode;
  autoFocus?: boolean;
  name?: string;
  required?: boolean;
  id?: string;
} & Omit<React.InputHTMLAttributes<HTMLInputElement>, "onChange" | "value">) {
  const [focus, setFocus] = useState(false);
  const inputId = id || name;
  const describedBy = error ? `${inputId}-err` : hint ? `${inputId}-hint` : undefined;
  return (
    <label style={{ display: "block" }} htmlFor={inputId}>
      {label && (
        <div style={{ fontSize: 13, fontWeight: 600, color: "var(--ink)", marginBottom: 7 }}>{label}</div>
      )}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 9,
          padding: "0 12px",
          height: 46,
          borderRadius: 10,
          background: "var(--surface)",
          border: `1.5px solid ${error ? "var(--danger)" : focus ? "var(--accent)" : "var(--border)"}`,
          boxShadow: focus ? "0 0 0 3.5px var(--accent-glow)" : "none",
          transition: "border-color .15s, box-shadow .15s",
        }}
      >
        {icon && <Icon name={icon} size={18} style={{ color: "var(--ink-3)" }} />}
        <input
          id={inputId}
          name={name}
          type={type}
          value={value}
          placeholder={placeholder}
          autoFocus={autoFocus}
          required={required}
          aria-invalid={!!error}
          aria-describedby={describedBy}
          onChange={(e) => onChange && onChange(e.target.value)}
          onFocus={() => setFocus(true)}
          onBlur={() => setFocus(false)}
          style={{
            flex: 1,
            border: "none",
            outline: "none",
            background: "transparent",
            fontSize: 14.5,
            fontFamily: "inherit",
            color: "var(--ink)",
            minWidth: 0,
          }}
          {...rest}
        />
        {right}
      </div>
      {error && (
        <div
          id={`${inputId}-err`}
          style={{ fontSize: 12.5, color: "var(--danger)", marginTop: 6, display: "flex", gap: 5, alignItems: "center" }}
        >
          <Icon name="alert" size={14} stroke={2} />
          {error}
        </div>
      )}
      {hint && !error && (
        <div id={`${inputId}-hint`} style={{ fontSize: 12.5, color: "var(--ink-3)", marginTop: 6 }}>
          {hint}
        </div>
      )}
    </label>
  );
}
