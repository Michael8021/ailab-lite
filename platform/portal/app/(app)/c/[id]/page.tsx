"use client";

import { useParams } from "next/navigation";
import Chat from "../../../../components/chat/chat";

export default function ConversationPage() {
  const params = useParams<{ id: string }>();
  return <Chat id={String(params?.id || "")} />;
}
