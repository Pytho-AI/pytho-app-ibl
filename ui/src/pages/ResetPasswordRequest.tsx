"use client";

import React, { useState } from "react";
import { useAppContext } from "../lib/contextLib";
import { IconWithBackground } from "../subframe/components/IconWithBackground";
import { TextField } from "../subframe/components/TextField";
import { Button } from "../subframe/components/Button";
import { useNavigate } from "react-router-dom";

function ResetPasswordRequest() {
  const { setUser } = useAppContext();
  const [isLoading, setIsLoading] = useState(false);
  const [email, setEmail] = useState("");
  const [emailError, setEmailError] = useState("");
  const [password, setPassword] = useState("");
  const [passwordError, setPasswordError] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [confirmPasswordError, setConfirmPasswordError] = useState("");
  const nav = useNavigate();

  async function handleReset() {
    let error = false;
    if (email.length < 1) {
      error = true;
      setEmailError("Email is required");
    }
    if (password.length < 1) {
      error = true;
      setPasswordError("New password is required");
    }
    if (confirmPassword !== password) {
      error = true;
      setConfirmPasswordError("Passwords do not match");
    }
    if (error) return;

    setIsLoading(true);
    try {
      const response = await fetch("/api/reset_password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password }),
      });
      if (response.ok) {
        const user = await response.json();
        setUser(user);
        localStorage.setItem("user", JSON.stringify(user));
        nav("/missioncommand");
      } else {
        const err = await response.json().catch(() => ({}));
        setEmailError(err.detail || "Password reset failed");
      }
    } catch (e) {
      setEmailError(e instanceof Error ? e.message : "Password reset failed");
    }
    setIsLoading(false);
  }

  return (
    <div className="container max-w-none flex h-full w-full flex-col items-center justify-center gap-4 bg-default-background py-12">
      <div className="flex max-w-[320px] flex-col items-center justify-center gap-6">
        <IconWithBackground size="large" icon="FeatherShieldQuestion" />
        <div className="flex flex-col items-center gap-2">
          <span className="text-heading-2 font-heading-2 text-default-font text-center">
            Reset your password
          </span>
          <span className="text-body font-body text-subtext-color text-center">
            Enter your email and a new password.
          </span>
        </div>
        <TextField
          className="h-auto w-full flex-none"
          label=""
          helpText={emailError}
          error={emailError.length > 0}
        >
          <TextField.Input
            placeholder="Email address"
            value={email}
            onChange={(event: React.ChangeEvent<HTMLInputElement>) => {
              setEmailError("");
              setEmail(event.target.value);
            }}
          />
        </TextField>
        <TextField
          className="h-auto w-full flex-none"
          label=""
          helpText={passwordError}
          error={passwordError.length > 0}
        >
          <TextField.Input
            type="password"
            placeholder="New password"
            value={password}
            onChange={(event: React.ChangeEvent<HTMLInputElement>) => {
              setPasswordError("");
              setPassword(event.target.value);
            }}
          />
        </TextField>
        <TextField
          className="h-auto w-full flex-none"
          label=""
          helpText={confirmPasswordError}
          error={confirmPasswordError.length > 0}
        >
          <TextField.Input
            type="password"
            placeholder="Confirm new password"
            value={confirmPassword}
            onChange={(event: React.ChangeEvent<HTMLInputElement>) => {
              setConfirmPasswordError("");
              setConfirmPassword(event.target.value);
            }}
          />
        </TextField>
        <div className="flex w-full flex-col items-start gap-2">
          <Button
            className="h-8 w-full flex-none"
            loading={isLoading}
            onClick={() => handleReset()}
          >
            Reset Password
          </Button>
          <Button
            className="h-8 w-full flex-none"
            variant="brand-tertiary"
            onClick={(event: React.MouseEvent<HTMLButtonElement>) => {
              event.preventDefault();
              window.location.href = "/signin";
            }}
          >
            Back to login
          </Button>
        </div>
      </div>
    </div>
  );
}

export default ResetPasswordRequest;
