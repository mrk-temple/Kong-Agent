import * as Dialog from "@radix-ui/react-dialog";
import type {ReactNode} from "react";
import {X} from "lucide-react";
export function Modal({title,description,children,onClose}:{title:string;description:string;children:ReactNode;onClose:()=>void}){return <Dialog.Root open onOpenChange={open=>{if(!open)onClose();}}><Dialog.Portal><Dialog.Overlay className="dialog-overlay"/><Dialog.Content className="dialog-content"><Dialog.Title className="dialog-title">{title}</Dialog.Title><Dialog.Description className="muted">{description}</Dialog.Description><Dialog.Close className="dialog-close" aria-label="关闭"><X size={18}/></Dialog.Close>{children}</Dialog.Content></Dialog.Portal></Dialog.Root>}
