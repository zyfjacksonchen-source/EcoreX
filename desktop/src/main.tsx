import React from "react";
import ReactDOM from "react-dom/client";
import { AppV1 } from "./v1/AppV1";
import "./styles/tokens.css";

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <AppV1 />
  </React.StrictMode>
);
