"use client";

import React, { useState }  from "react";
import * as SubframeCore from "@subframe/core";
import { TextField } from "../subframe/components/TextField";
import { Button } from "../subframe/components/Button";
import { LinkButton } from "../subframe/components/LinkButton";
import { useAppContext } from "../lib/contextLib";
import { useNavigate } from "react-router-dom";
import { useTheme } from "../lib/themeLib";

function SignUp() {
  const { setUser } = useAppContext();
  const { theme } = useTheme();
  const logoSrc = theme === 'light' ? '/ggvf8l4lhv21z3kgovuv-light.png' : '/ggvf8l4lhv21z3kgovuv.png';
  const [email, setEmail] = useState("");
  const [emailError, setEmailError] = useState("");
  const [password, setPassword] = useState("");
  const [passwordError, setPasswordError] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [confirmPasswordError, setConfirmPasswordError] = useState("");
  const [firstName, setFirstName] = useState("");
  const [firstNameError, setFirstNameError] = useState("");
  const [lastName, setLastName] = useState("");
  const [lastNameError, setLastNameError] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const nav = useNavigate();

  async function handleSignUp() {
    let error = false
    if (firstName.length < 1) {
      error = true
      setFirstNameError("First name is required")
    }
    if (lastName.length < 1) {
      error = true
      setLastNameError("Last name is required")
    }
    if (email.length < 1) {
      error = true
      setEmailError("Email is required")
    }
    if (password.length < 1) {
      error = true
      setPasswordError("Password is required")
    }
    if (confirmPassword.length < 1) {
      error = true
      setConfirmPasswordError("Confirm Password is required")
    }
    if (confirmPassword !== password) {
      error = true
      setConfirmPasswordError("Passwords do not match")
    }

    if (error) return

    setIsLoading(true);

    try {
      const response = await fetch("/api/signup", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password, firstName, lastName }),
      });
      if (response.ok) {
        const user = await response.json();
        setUser(user);
        localStorage.setItem("user", JSON.stringify(user));
        nav("/missioncommand");
      } else {
        const err = await response.json().catch(() => ({}));
        setEmailError(err.detail || "Sign up failed");
      }
    } catch (e) {
      setEmailError(e instanceof Error ? e.message : "Sign up failed");
    }

    setIsLoading(false);

  }

  return (
    <div className="flex h-full w-full flex-wrap items-center justify-center gap-12 bg-default-background px-12 py-12 mobile:flex-col mobile:flex-wrap mobile:gap-12 mobile:px-6 mobile:py-12">
      <div className="flex max-w-[576px] grow shrink-0 basis-0 flex-col items-center justify-center gap-12 self-stretch mobile:h-auto mobile:w-full mobile:max-w-[576px] mobile:flex-none">
        <img
          className="h-16 w-56 flex-none object-cover"
          src={logoSrc}
        />
        <div className="flex flex-col items-center justify-center gap-6 px-12 mobile:flex mobile:px-0 mobile:py-0">
          <div className="flex w-full items-start justify-center gap-4 px-2 py-2">
            <SubframeCore.Icon
              className="text-heading-2 font-heading-2 text-brand-700"
              name="FeatherLightbulb"
            />
            <div className="flex flex-col items-start gap-1">
              <span className="w-full text-heading-3 font-heading-3 text-brand-700">
                Create better wargaming products
              </span>
              <span className="w-full text-body font-body text-subtext-color">
                Vast wargaming knowledge at your fingertips to create the best
                and most realistic wargaming products imaginable. 
              </span>
            </div>
          </div>
          <div className="flex items-start justify-center gap-4 px-2 py-2">
            <SubframeCore.Icon
              className="text-heading-2 font-heading-2 text-brand-700"
              name="FeatherRocket"
            />
            <div className="flex flex-col items-start gap-1">
              <span className="text-heading-3 font-heading-3 text-brand-700">
                Simplify your workflow
              </span>
              <span className="text-body font-body text-subtext-color">
                Pytho integrates into your existing processes to give you the
                power of a superhuman wargaming  expert. 
              </span>
            </div>
          </div>
          <div className="flex items-start justify-center gap-4 px-2 py-2">
            <SubframeCore.Icon
              className="text-heading-2 font-heading-2 text-brand-700"
              name="FeatherZap"
            />
            <div className="flex flex-col items-start gap-1">
              <span className="text-heading-3 font-heading-3 text-brand-700">
                Scale your operations
              </span>
              <span className="text-body font-body text-subtext-color">
                Plan and execute ten events in the time that it used to take to
                plan and execute one. 
              </span>
            </div>
          </div>
        </div>
      </div>
      <div className="flex max-w-[448px] grow shrink-0 basis-0 flex-col items-center justify-center gap-6 rounded-md border border-solid border-neutral-border bg-default-background px-12 py-12 shadow-lg">
        <div className="flex w-full flex-col items-center justify-center gap-8">
          <span className="w-full text-heading-3 font-heading-3 text-default-font">
            Create your Pytho account
          </span>
          <div className="flex w-full flex-col items-start justify-center gap-6">
            <TextField
              className="h-auto w-full flex-none"
              label="First Name"
              helpText={firstNameError}
              error={firstNameError.length > 0}
            >
              <TextField.Input
                placeholder=""
                value={firstName}
                onChange={(event: React.ChangeEvent<HTMLInputElement>) => {
                  setFirstNameError("")
                  setFirstName(event.target.value)
                }}
                onKeyUp={async(event: React.MouseEvent<HTMLButtonElement>) => {
                  if (event.keyCode === 13) {
                    handleSignUp()  
                  }
                }}
              />
            </TextField>
            <TextField
              className="h-auto w-full flex-none"
              label="Last Name"
              helpText={lastNameError}
              error={lastNameError.length > 0}
            >
              <TextField.Input
                placeholder=""
                value={lastName}
                onChange={(event: React.ChangeEvent<HTMLInputElement>) => {
                  setLastNameError("")
                  setLastName(event.target.value)
                }}
                onKeyUp={async(event: React.MouseEvent<HTMLButtonElement>) => {
                  if (event.keyCode === 13) {
                    handleSignUp()  
                  }
                }}
              />
            </TextField>
            <TextField
              className="h-auto w-full flex-none"
              label="Email"
              helpText={emailError}
              error={emailError.length > 0}
            >
              <TextField.Input
                placeholder=""
                value={email}
                onChange={(event: React.ChangeEvent<HTMLInputElement>) => {
                  setEmailError("")
                  setEmail(event.target.value)
                }}
                onKeyUp={async(event: React.MouseEvent<HTMLButtonElement>) => {
                  if (event.keyCode === 13) {
                    handleSignUp()  
                  }
                }}
              />
            </TextField>
            <TextField
              className="h-auto w-full flex-none"
              label="Password"
              helpText={passwordError}
              error={passwordError.length > 0}
            >
              <TextField.Input
                type="password"
                placeholder=""
                value={password}
                onChange={(event: React.ChangeEvent<HTMLInputElement>) => {
                  setPasswordError("")
                  setPassword(event.target.value)
                }}
                onKeyUp={async(event: React.MouseEvent<HTMLButtonElement>) => {
                  if (event.keyCode === 13) {
                    handleSignUp()  
                  }
                }}
              />
            </TextField>
            <TextField
              className="h-auto w-full flex-none"
              label="Confirm Password"
              helpText={confirmPasswordError}
              error={confirmPasswordError.length > 0}
            >
              <TextField.Input
                type="password"
                placeholder=""
                value={confirmPassword}
                onChange={(event: React.ChangeEvent<HTMLInputElement>) => {
                  setConfirmPasswordError("")
                  setConfirmPassword(event.target.value)
                }}
                onKeyUp={async(event: React.MouseEvent<HTMLButtonElement>) => {
                  if (event.keyCode === 13) {
                    handleSignUp()  
                  }
                }}
              />
            </TextField>
          </div>
          <Button
            className="h-10 w-full flex-none"
            disabled={false}
            variant="brand-primary"
            size="large"
            icon={null}
            iconRight={null}
            loading={isLoading}
            onClick={async(event: React.MouseEvent<HTMLButtonElement>) => {
              handleSignUp() 
            }}
          >
            Create account
          </Button>
          <div className="flex flex-wrap items-center gap-1">
            <span className="text-body font-body text-default-font">
              Have an account?
            </span>
            <LinkButton
              variant="brand"
              size="medium"
              icon={null}
              onClick={(event: React.MouseEvent<HTMLButtonElement>) => {
                event.preventDefault();
                window.location.href='/signin';
              }}
            >
              Sign In
            </LinkButton>
          </div>
        </div>
      </div>
    </div>
  );
}

export default SignUp;