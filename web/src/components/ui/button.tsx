import type {ButtonHTMLAttributes} from "react";
import {cva} from "class-variance-authority";
import {clsx} from "clsx";
const variants=cva("button",{variants:{variant:{default:"button-primary",outline:"button-outline",ghost:"button-ghost",danger:"button-danger"}},defaultVariants:{variant:"default"}});
export function Button({variant="default",className,...props}:ButtonHTMLAttributes<HTMLButtonElement>&{variant?:"default"|"outline"|"ghost"|"danger"}){return <button className={clsx(variants({variant}),className)} {...props}/>}
