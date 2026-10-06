export function isLocalHost(hostname: string): boolean;

export function computeApiBase(params?: {
  envApiBase?: string | null | undefined;
  hostname?: string | undefined;
  port?: string | undefined;
  protocol?: string | undefined;
}): string;
