"use client";

import { useParams } from "next/navigation";
import Chat from "../../../../components/chat/chat";

export default function ConversationPage() {
  const params = useParams<{ id: string }>();
  const id = String(params?.id || "");
  return <Chat key={id} id={id} />;
}
