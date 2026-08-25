"use client";

import React, { useState }  from "react";
import { TextField } from "../subframe/components/TextField";
import { Button } from "../subframe/components/Button";
import { LinkButton } from "../subframe/components/LinkButton";
import { useAppContext } from "../lib/contextLib";
import { useNavigate } from "react-router-dom";
import { useTheme } from "../lib/themeLib";

function SignIn() {
  const { setUser } = useAppContext();
  const { theme } = useTheme();
  const logoSrc = theme === 'light' ? '/ggvf8l4lhv21z3kgovuv-light.png' : '/ggvf8l4lhv21z3kgovuv.png';
  const [email, setEmail] = useState("");
  const [emailError, setEmailError] = useState("");
  const [password, setPassword] = useState("");
  const [passwordError, setPasswordError] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const nav = useNavigate();

  async function handleSignIn() {
    let error = false
    if (email.length < 1) {
      error = true
      setEmailError("Email is required")
    }
    if (password.length < 1) {
      error = true
      setPasswordError("Password is required")
    }

    if (error) return

    setIsLoading(true);

    try {
      const requestOptions = {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ 
          email,  
          password 
        })
      };

      const response = await fetch("/api/auth", requestOptions)
      
      if (response.ok) {
        const user = await response.json()
        setUser(user)
        localStorage.setItem("user", JSON.stringify(user));
        nav("/missioncommand");
      } else {
        const error = await response.json()
        setEmailError(error.detail)
      }
    } catch (e) {
      if (e instanceof Error) {
        const message = e.message.replace('Password did not conform with policy: ', '').replace(/,+$/, "")
        if (e.name === 'InvalidPasswordException') {
          setPasswordError(message);  
        } else {
          setEmailError(message);
        }              
      }
    }

    setIsLoading(false);
  }
  
  return (
    <div className="container max-w-none flex h-full w-full flex-col items-center justify-center gap-2 bg-default-background">
      <div className="flex w-full max-w-[384px] flex-col items-center justify-center gap-8 rounded-md border border-solid border-neutral-border bg-neutral-50 px-12 py-12">
        <img
          className="h-11 w-40 flex-none object-cover"
          src={logoSrc}
        />
        <span className="text-body font-body text-default-font">
          Welcome Back!
        </span>
        <div className="flex w-full flex-col items-start gap-6">
          <div className="flex w-full flex-col items-start gap-6">
            <TextField
              className="h-auto w-full flex-none"
              variant="outline"
              label="Email"
              helpText={emailError}
              error={emailError.length > 0}
            >
              <TextField.Input
                placeholder="Your email address..."
                value={email}
                onChange={(event: React.ChangeEvent<HTMLInputElement>) => {
                  setEmailError("")
                  setEmail(event.target.value)
                }}
                onKeyUp={async(event: React.MouseEvent<HTMLButtonElement>) => {
                  if (event.keyCode === 13) {
                    handleSignIn()  
                  }
                }}
              />
            </TextField>
            <TextField
              className="h-auto w-full flex-none"
              variant="outline"
              label="Password"
              helpText={passwordError}
              error={passwordError.length > 0}
            >
              <TextField.Input
                type="password"
                placeholder="Your password..."
                value={password}
                onChange={(event: React.ChangeEvent<HTMLInputElement>) => {
                  setPasswordError("")
                  setPassword(event.target.value)
                }}
                onKeyUp={async(event: React.MouseEvent<HTMLButtonElement>) => {
                  if (event.keyCode === 13) {
                    handleSignIn()  
                  }
                }}
              />
            </TextField>
          </div>
        </div>
        <Button
          className="h-10 w-full flex-none"
          size="large"
          icon={null}
          loading={isLoading}
          onClick={async(event: React.MouseEvent<HTMLButtonElement>) => {
            handleSignIn()  
          }}
        >
          Sign in
        </Button>
        <div className="flex flex-wrap items-center gap-1">
          <span className="text-body font-body text-default-font">
            Forgot Password?
          </span>
          <LinkButton
            variant="brand"
            size="medium"
            icon={null}
            onClick={(event: React.MouseEvent<HTMLButtonElement>) => {
              event.preventDefault();
              window.location.href='/reset';
            }}
          >
            Reset
          </LinkButton>
        </div>
        <div className="flex flex-wrap items-center gap-1">
          <span className="text-body font-body text-default-font">
            Don&#39;t have an account?
          </span>
          <LinkButton
            variant="brand"
            size="medium"
            icon={null}
            onClick={(event: React.MouseEvent<HTMLButtonElement>) => {
              event.preventDefault();
              window.location.href='/signup';
            }}
          >
            Sign Up
          </LinkButton>
        </div>
      </div>
    </div>
  );
}

export default SignIn;