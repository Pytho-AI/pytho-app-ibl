import { ReactElement, cloneElement } from "react";
import { Navigate } from "react-router-dom";
import { useAppContext } from "../lib/contextLib";
import { querystring } from "../lib/urlLib";

interface Props {
  children: ReactElement;
}

export default function UnauthenticatedRoute(props: Props): ReactElement {
  const { children } = props;
  const { user } = useAppContext();
  const redirect = querystring("redirect");
  
  if (user) {
    return <Navigate to={redirect || "/missioncommand"} />;
  }

  return cloneElement(children, props);
}
