import StudioApp from "../../../features/studio/StudioApp";

export default async function StudioProjectPage({
  params,
}: {
  params: Promise<{ projectId: string }>;
}) {
  const { projectId: rawId } = await params;
  const id = decodeURIComponent(rawId).trim();

  if (id === "") {
    return <StudioApp projectId={null} invalid />;
  }

  return <StudioApp projectId={id} />;
}
