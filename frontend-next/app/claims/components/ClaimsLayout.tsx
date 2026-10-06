import type { ReactNode } from "react";

export const claimsLayout = {
  shell: "!grid !content-start !gap-3",
  topbar: "!m-0",
  // 工作区网格列由 globals.css 末尾的覆写块统一定义（左栏 360px 写死，无断点切换）
  workspace: "!mt-0",
  column: "flex min-w-0 flex-col gap-3",
  controls: "!grid !h-auto !min-h-[440px] !content-start !gap-3 [&>*]:!m-0",
  lower: "col-span-full grid min-w-0 content-start gap-3",
  downloads: "!grid !grid-cols-2 !gap-2 [&>*]:!m-0",
  kpis: "!grid !grid-cols-2 !gap-2",
} as const;

export function ClaimsStageStack({ children }: { children: ReactNode }) {
  return (
    <div className="grid min-w-0 content-start gap-3 [&>*]:!m-0">
      {children}
    </div>
  );
}
