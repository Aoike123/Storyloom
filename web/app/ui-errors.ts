export function uiErrorMessage(
  error: unknown,
  fallback = "暂时无法完成操作，请稍后再试。",
): string {
  const message =
    typeof error === "string"
      ? error
      : error && typeof error === "object" && "message" in error
        ? String(error.message)
        : "";
  if (
    /^(failed to fetch|fetch failed|load failed|networkerror.*|network request failed)$/i.test(
      message,
    )
  ) {
    return "无法连接本地服务，请确认服务正在运行后重试。";
  }
  if (/unexpected (token|end)|not valid json/i.test(message)) return fallback;
  return message || fallback;
}
