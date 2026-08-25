"use client";

import React, { useState } from "react";
import { useTheme } from "../lib/themeLib";
import { Button } from "../subframe/components/Button";
import { IconWithBackground } from "../subframe/components/IconWithBackground";
import { TextField } from "../subframe/components/TextField";
import { TextArea } from "../subframe/components/TextArea";
import { API } from "aws-amplify";
import { ListingsPageDetail } from "../subframe/components/ListingsPageDetail";
import * as SubframeCore from "@subframe/core";


function scrollToDiv(divId) {
  const element = document.getElementById(divId);
  if (element) {
    element.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
}

function Root() {
  const { theme } = useTheme();
  const logoSrc = theme === 'light' ? '/ggvf8l4lhv21z3kgovuv-light.png' : '/ggvf8l4lhv21z3kgovuv.png';
  const [firstName, setFirstName] = useState("")
  const [firstNameError, setFirstNameError] = useState("")
  const [lastName, setLastName] = useState("")
  const [lastNameError, setLastNameError] = useState("")
  const [email, setEmail] = useState("")
  const [emailError, setEmailError] = useState("")
  const [organization, setOrganization] = useState("")
  const [organizationError, setOrganizationError] = useState("")
  const [message, setMessage] = useState("")
  const [messageError, setMessageError] = useState("")
  const [messageSent, setMessageSent] = useState("")

  return (
    <div className="max-w-none flex w-full flex-col items-start h-screen overflow-auto">
      <div className="flex w-full flex-col items-start bg-black">
        <div className="flex w-full flex-col items-start justify-center bg-default-background px-6 py-6">
          <div className="flex w-full items-center justify-between cursor-pointer">
            <img
              className="h-8 flex-none object-cover"
              onClick={() => window.location = '/'}
              src={logoSrc}
            />
            <div className="flex items-center gap-6">
              <a className="text-body font-body text-default-font hover:underline cursor-pointer" onClick={() => scrollToDiv('touch')}>
                Contact
              </a>
              <a className="text-body font-body text-default-font hover:underline cursor-pointer" onClick={() => window.open("https://chain-interest-92a.notion.site/Founding-AI-Engineer-18539078df5c805cb4f1d9e2d4b97b61", "_blank")}>
                Careers
              </a>
            </div>
          </div>
        </div>
        <div className="flex min-h-[768px] w-full flex-col items-center justify-center gap-12 overflow-hidden relative">
          <div className="flex flex-col items-center justify-center gap-8 z-10">
            <div className="flex w-full max-w-[768px] flex-col items-center gap-4">
              <span className="w-full font-['Inter'] text-[72px] font-[600] leading-[78px] text-default-font text-center -tracking-[0.035em]">
                Speed is Everything
              </span>
              <span className="w-full whitespace-pre-wrap font-['Inter'] text-[24px] font-[100] leading-[32px] text-default-font text-center -tracking-[0.025em]">
                {
                  "We are redefining force readiness for the modern battlefield."
                }
              </span>
            </div>
            <Button
              variant="neutral-secondary"
              size="large"
              onClick={() => scrollToDiv('touch')}
            >
              Learn more
            </Button>
          </div>
          <img
            className="w-full grow shrink-0 basis-0 object-cover absolute inset-0 opacity-20"
            src="/aziisselzeuc4bj7vl6a.gif"
          />
          <div className="flex w-full flex-col items-start bg-black/20 absolute inset-0" />
        </div>
        <div id="touch" className="flex w-full flex-col items-center justify-center bg-default-background px-4 py-16">
          <div className="flex w-full max-w-[1024px] flex-col items-center gap-6 bg-default-background">
            <div className="flex flex-col items-center gap-4">
              <IconWithBackground size="large" icon="FeatherMail" />
              <span className="font-['Inter'] text-[48px] font-[700] leading-[48px] text-default-font text-center -tracking-[0.035em]">
                Get in touch
              </span>
              <span className="max-w-[448px] font-['Inter'] text-[20px] font-[100] leading-[28px] text-subtext-color text-center">
                Let us know how we can best support your team
              </span>
            </div>
            <div className="flex w-full max-w-[448px] flex-col items-start gap-8 rounded-xl border border-solid border-neutral-border bg-neutral-50 px-4 md:px-8 py-8">
              <div className="flex w-full items-start gap-8 md:gap-6 flex-col md:flex-row">
                <TextField
                  className="h-auto grow shrink-0 basis-0 w-full"
                  variant="filled"
                  label="First name"
                  error={firstNameError.length > 0}
                  icon="FeatherUser"
                >
                  <TextField.Input
                    placeholder="First name"
                    value={firstName}
                    onChange={(
                      event: React.ChangeEvent<HTMLTextAreaElement>
                    ) => {
                      setFirstName(event.target.value)
                      setMessageSent("")
                      setFirstNameError("")
                    }}
                  />
                </TextField>
                <TextField
                  className="h-auto grow shrink-0 basis-0 w-full"
                  variant="filled"
                  label="Last name"
                  error={lastNameError.length > 0}
                  icon="FeatherUser"
                >
                  <TextField.Input
                    placeholder="Last name"
                    value={lastName}
                    onChange={(
                      event: React.ChangeEvent<HTMLTextAreaElement>
                    ) => {
                      setLastName(event.target.value)
                      setLastNameError("")
                      setMessageSent("")
                    }}
                  />
                </TextField>
              </div>
              <TextField
                className="h-auto w-full flex-none"
                variant="filled"
                label="Work email"
                error={emailError.length > 0}  
                icon="FeatherMail"
              >
                <TextField.Input
                  placeholder="email@company.com"
                  value={email}
                  onChange={(
                    event: React.ChangeEvent<HTMLTextAreaElement>
                  ) => {
                    setEmail(event.target.value)
                    setEmailError("")
                    setMessageSent("")
                  }}
                />
              </TextField>
              <TextField
                className="h-auto w-full flex-none"
                variant="filled"
                label="Organization"
                error={organizationError.length > 0}
                icon="FeatherBriefcase"
              >
                <TextField.Input
                  placeholder="Team name"
                  value={organization}
                  onChange={(
                    event: React.ChangeEvent<HTMLTextAreaElement>
                  ) => {
                    setOrganization(event.target.value)
                    setMessageSent("")
                    setOrganizationError("")
                  }}
                />
              </TextField>
              <TextArea
                className="h-auto w-full flex-none"
                variant="filled"
                label="Message"
                helpText={messageSent}
                error={messageError.length > 0}
              >
                <TextArea.Input
                  placeholder="Your message"
                  className="resize-none overflow-y-hidden"
                  value={message}
                  onChange={(
                    event: React.ChangeEvent<HTMLTextAreaElement>
                  ) => {
                    setMessage(event.target.value)
                    setMessageSent("")
                    setMessageError("")
                  }}
                />
              </TextArea>
              <Button
                className="h-12 w-full flex-none"
                size="large"
                onClick={async(event: React.MouseEvent<HTMLButtonElement>) => {
                  let error = false
                  
                  if (firstName.length < 1) {
                    setFirstNameError('First name is missing')
                    error = true
                  }

                  if (lastName.length < 1) {
                    setLastNameError('Last name is missing')
                    error = true
                  }

                  if (email.length < 3) {
                    setEmailError('A valid email address is needed')
                    error = true
                  }

                  if (organization.length < 1) {
                    setOrganizationError('Organization is required')
                    error = true
                  }

                  if (message.length < 1) {
                    setMessageError('Message is required')
                    error = true
                  }

                  if (error) {
                    return
                  }
                  setMessageSent("Thank you for reaching out! We'll get back to you soon.")
                  await API.post("feedback", `/chat`, {
                    body: {
                      email: email,
                      message: `
Name: ${firstName} ${lastName}
Email: ${email}
Organization: ${organization}
Message: ${message}`,
                    }
                  });
                }}
              >
                Send message
              </Button>
            </div>
          </div>
        </div>
        <div className="flex w-full flex-col items-center justify-center bg-neutral-50 px-4 py-14">
          <div className="flex w-full md:w-256 items-center justify-between rounded-2xl border border-solid border-neutral-border bg-default-background px-4 md:px-16 py-12">
            <div className="flex max-w-[448px] flex-col items-start gap-6 pr-6">
              <span className="font-['Inter'] text-[36px] font-[600] leading-[40px] text-brand-800 -tracking-[0.02em]">
                Join Pytho.
              </span>
              <span className="font-['Inter'] text-[20px] font-[100] leading-[28px] text-default-font -tracking-[0.015em]">
                Want to join our team? If you&#39;re an AI Engineer passionate
                about National Security, send us your CV and why you want to
                work with us.
              </span>
              <Button
                size="large"
                iconRight="FeatherArrowRight"
                onClick={() => window.location = 'mailto:team@pytho.ai'}
              >
                Email us
              </Button>
            </div>
            <div>
              <IconWithBackground
                size="x-large"
                icon="FeatherRocket"
                square={true}
              />
            </div>
          </div>
        </div>
        <div className="flex w-full flex-col items-center bg-black px-6 py-12">
          <div className="flex w-full flex-col items-start gap-8">
            <div className="flex h-px w-full flex-none items-start bg-neutral-border" />
            <ListingsPageDetail icon={null} label="">
              <span className="text-heading-1 font-heading-1 text-default-font">
                Pytho AI
              </span>
              <span className="w-full whitespace-pre-wrap text-body font-body text-default-font">
                {"San Francisco, CA\nWashington, DC"}
              </span>
              <div className="flex items-center gap-2 mt-2 hover:cursor-pointer">
                <SubframeCore.Icon
                  className="text-heading-1 font-heading-1 text-subtext-color"
                  name="FeatherLinkedin"
                  onClick={() => window.open("https://www.linkedin.com/company/pytho-ai", "_blank")}
                />
              </div>
            </ListingsPageDetail>
            <div className="flex w-full flex-col items-center gap-6">
              <span className="text-body font-body text-subtext-color">
                © 2025 Pytho AI, Inc.
              </span>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

export default Root;