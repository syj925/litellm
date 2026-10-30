"use client";

import useAuthorized from "@/app/(dashboard)/hooks/useAuthorized";
import ModelCatalogBrowser from "./catalog/ModelCatalogBrowser";

export default function UserModelCatalog() {
  const { accessToken, userId } = useAuthorized();
  return <ModelCatalogBrowser accessToken={accessToken} userId={userId} />;
}
