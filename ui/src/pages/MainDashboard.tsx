"use client";

import { Button } from "../subframe/components/Button";
import { DefaultPageLayout } from "../subframe/layouts/DefaultPageLayout";
import * as SubframeCore from "@subframe/core";
import { useNavigate } from "react-router-dom";
import { useTheme } from "../lib/themeLib";

function MainDashboard() {
  const nav = useNavigate();
  const { theme } = useTheme();
  const logoSrc = theme === 'light' ? '/ggvf8l4lhv21z3kgovuv-light.png' : '/ggvf8l4lhv21z3kgovuv.png';

   return (
    <DefaultPageLayout>
      <div className="flex h-full min-h-0 w-full flex-1 flex-col items-center justify-center overflow-hidden gap-8 px-6 py-6 bg-gradient-to-br from-neutral-0 via-default-background to-neutral-50">
        <div className="flex flex-shrink-0 flex-col items-center gap-3">
          <img
            className="h-14 flex-none object-cover opacity-90"
            src={logoSrc}
          />
          <span className="font-['Afacad_Flux'] text-[28px] font-[700] leading-[34px] tracking-tight text-default-font">
            Wargame Operations Center
          </span>
        </div>
        <div className="flex w-full max-w-[1100px] gap-6 items-stretch flex-shrink-0">
          <div className="flex grow shrink-0 basis-0 flex-col items-center justify-center gap-5 rounded-lg px-10 py-12 border-2 border-solid border-brand-primary/20 bg-gradient-to-b from-neutral-50/10 to-neutral-100/5 backdrop-blur-xl hover:border-brand-primary/60 hover:shadow-brand-primary/20 hover:shadow-2xl transition-all duration-300">
            <div className="flex flex-col items-center gap-4">
              <div className="flex h-16 w-16 flex-none items-center justify-center rounded-full shadow-lg bg-brand-primary/10 border border-solid border-brand-primary/30 shadow-brand-primary/20">
                <SubframeCore.Icon
                  className="text-body font-body text-brand-primary"
                  name="FeatherSwords"
                />
              </div>
              <div className="flex flex-col items-center gap-2">
                <span className="font-['Afacad_Flux'] text-[24px] font-[700] leading-[30px] text-default-font">
                  White Cell
                </span>
                <span className="max-w-[240px] text-caption font-caption text-subtext-color text-center">
                  Design, plan, and orchestrate the end-to-end wargaming lifecycle
                </span>
              </div>
            </div>
            <div className="flex w-full flex-col items-center gap-2">
              <Button
                className="h-12 w-full flex-none text-[14px] shadow-lg shadow-brand-primary/20 group transition-all duration-300"
                size="large"
                icon="FeatherArrowRight"
                onClick={() => nav("/missioncommand/whitecell")}
              >
                Enter White Cell
              </Button>
            </div>
          </div>
          <div className="flex grow shrink-0 basis-0 flex-col items-center justify-center gap-5 rounded-lg px-10 py-12 border-2 border-solid border-neutral-border bg-gradient-to-b from-neutral-50/10 to-neutral-100/5 backdrop-blur-xl opacity-75">
            <div className="flex flex-col items-center gap-4">
              <div className="flex h-16 w-16 flex-none items-center justify-center rounded-full shadow-lg bg-neutral-200/80 border border-solid border-neutral-border">
                <SubframeCore.Icon
                  className="text-body font-body text-neutral-500"
                  name="FeatherShield"
                />
              </div>
              <div className="flex flex-col items-center gap-2">
                <span className="font-['Afacad_Flux'] text-[24px] font-[700] leading-[30px] text-default-font">
                  Participants
                </span>
                <span className="max-w-[240px] text-caption font-caption text-subtext-color text-center">
                  Execute wargames with your team
                </span>
              </div>
            </div>
            <div className="flex w-full flex-col items-center gap-2">
              <Button
                disabled
                className="h-12 w-full flex-none text-[14px] opacity-60 cursor-not-allowed"
                variant="neutral-primary"
                size="large"
                icon="FeatherArrowRight"
                onClick={() => {}}
              >
                Join as Participant
              </Button>
            </div>
          </div>
        </div>
        <div className="flex flex-shrink-0 items-center gap-6 opacity-60">
          <div className="flex items-center gap-2">
            <SubframeCore.Icon
              className="text-caption font-caption text-default-font"
              name="FeatherTarget"
            />
            <span className="text-caption font-caption text-default-font">
              Realistic
            </span>
          </div>
          <div className="flex h-3 w-px flex-none items-start bg-neutral-border" />
          <div className="flex items-center gap-2">
            <SubframeCore.Icon
              className="text-caption font-caption text-default-font"
              name="FeatherZap"
            />
            <span className="text-caption font-caption text-default-font">
              Dynamic
            </span>
          </div>
          <div className="flex h-3 w-px flex-none items-start bg-neutral-border" />
          <div className="flex items-center gap-2">
            <SubframeCore.Icon
              className="text-caption font-caption text-default-font"
              name="FeatherGlobe"
            />
            <span className="text-caption font-caption text-default-font">
              Multi-Domain
            </span>
          </div>
        </div>
      </div>
    </DefaultPageLayout>
  );
}

export default MainDashboard;