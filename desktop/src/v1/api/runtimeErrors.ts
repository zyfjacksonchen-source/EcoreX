export class RuntimeApiError extends Error {
  readonly status: number;
  readonly code: string | null;

  constructor(message: string, status: number, code: string | null = null) {
    super(message);
    this.name = "RuntimeApiError";
    this.status = status;
    this.code = code;
  }
}
