import { ReactElement } from "react";
import {  Navigate, useLocation } from "react-router-dom";
import { useAppContext } from "../lib/contextLib";

export default function AuthenticatedRoute({
  children,
}: {
  children: ReactElement;
}): ReactElement {
  const { pathname, search } = useLocation();
  const { user } = useAppContext();
  
  if (!user) {
    return <Navigate to={`/signin?redirect=${pathname}${search}`} />;
  }

  return children;
}
