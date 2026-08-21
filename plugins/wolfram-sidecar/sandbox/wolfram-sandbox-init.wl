(* wolfram-sandbox-init.wl — Kernel-level sandbox for wolframscript
 *
 * Loaded before user code. Overrides dangerous functions so they
 * return $Failed with a message. Works at the Wolfram language level,
 * so ToExpression["Run[\"ls\"]"] fails because Run itself is disabled.
 *)

General::sandbox = "`` is blocked in sandbox mode.";

(* === Override dangerous functions === *)
Quiet[Block[{$ContextPath},

(* Process execution *)
Unprotect[Run]; ClearAll[Run];
Run[___] := (Message[General::sandbox, "Run"]; $Failed);
Protect[Run]; SetAttributes[Run, Locked];

Unprotect[RunProcess]; ClearAll[RunProcess];
RunProcess[___] := (Message[General::sandbox, "RunProcess"]; $Failed);
Protect[RunProcess]; SetAttributes[RunProcess, Locked];

Unprotect[StartProcess]; ClearAll[StartProcess];
StartProcess[___] := (Message[General::sandbox, "StartProcess"]; $Failed);
Protect[StartProcess]; SetAttributes[StartProcess, Locked];

Unprotect[SystemOpen]; ClearAll[SystemOpen];
SystemOpen[___] := (Message[General::sandbox, "SystemOpen"]; $Failed);
Protect[SystemOpen]; SetAttributes[SystemOpen, Locked];

(* File write *)
Unprotect[Export]; ClearAll[Export];
Export[___] := (Message[General::sandbox, "Export"]; $Failed);
Protect[Export]; SetAttributes[Export, Locked];

Unprotect[Put]; ClearAll[Put];
Put[___] := (Message[General::sandbox, "Put"]; $Failed);
Protect[Put]; SetAttributes[Put, Locked];

Unprotect[PutAppend]; ClearAll[PutAppend];
PutAppend[___] := (Message[General::sandbox, "PutAppend"]; $Failed);
Protect[PutAppend]; SetAttributes[PutAppend, Locked];

Unprotect[OpenWrite]; ClearAll[OpenWrite];
OpenWrite[___] := (Message[General::sandbox, "OpenWrite"]; $Failed);
Protect[OpenWrite]; SetAttributes[OpenWrite, Locked];

Unprotect[OpenAppend]; ClearAll[OpenAppend];
OpenAppend[___] := (Message[General::sandbox, "OpenAppend"]; $Failed);
Protect[OpenAppend]; SetAttributes[OpenAppend, Locked];

Unprotect[DeleteFile]; ClearAll[DeleteFile];
DeleteFile[___] := (Message[General::sandbox, "DeleteFile"]; $Failed);
Protect[DeleteFile]; SetAttributes[DeleteFile, Locked];

Unprotect[RenameFile]; ClearAll[RenameFile];
RenameFile[___] := (Message[General::sandbox, "RenameFile"]; $Failed);
Protect[RenameFile]; SetAttributes[RenameFile, Locked];

Unprotect[CopyFile]; ClearAll[CopyFile];
CopyFile[___] := (Message[General::sandbox, "CopyFile"]; $Failed);
Protect[CopyFile]; SetAttributes[CopyFile, Locked];

Unprotect[CreateFile]; ClearAll[CreateFile];
CreateFile[___] := (Message[General::sandbox, "CreateFile"]; $Failed);
Protect[CreateFile]; SetAttributes[CreateFile, Locked];

Unprotect[BinaryWrite]; ClearAll[BinaryWrite];
BinaryWrite[___] := (Message[General::sandbox, "BinaryWrite"]; $Failed);
Protect[BinaryWrite]; SetAttributes[BinaryWrite, Locked];

Unprotect[WriteString]; ClearAll[WriteString];
WriteString[___] := (Message[General::sandbox, "WriteString"]; $Failed);
Protect[WriteString]; SetAttributes[WriteString, Locked];

(* File read *)
Unprotect[ReadString]; ClearAll[ReadString];
ReadString[___] := (Message[General::sandbox, "ReadString"]; $Failed);
Protect[ReadString]; SetAttributes[ReadString, Locked];

Unprotect[ReadList]; ClearAll[ReadList];
ReadList[___] := (Message[General::sandbox, "ReadList"]; $Failed);
Protect[ReadList]; SetAttributes[ReadList, Locked];

Unprotect[FilePrint]; ClearAll[FilePrint];
FilePrint[___] := (Message[General::sandbox, "FilePrint"]; $Failed);
Protect[FilePrint]; SetAttributes[FilePrint, Locked];

(* Network *)
Unprotect[URLFetch]; ClearAll[URLFetch];
URLFetch[___] := (Message[General::sandbox, "URLFetch"]; $Failed);
Protect[URLFetch]; SetAttributes[URLFetch, Locked];

Unprotect[URLRead]; ClearAll[URLRead];
URLRead[___] := (Message[General::sandbox, "URLRead"]; $Failed);
Protect[URLRead]; SetAttributes[URLRead, Locked];

Unprotect[URLExecute]; ClearAll[URLExecute];
URLExecute[___] := (Message[General::sandbox, "URLExecute"]; $Failed);
Protect[URLExecute]; SetAttributes[URLExecute, Locked];

Unprotect[SendMail]; ClearAll[SendMail];
SendMail[___] := (Message[General::sandbox, "SendMail"]; $Failed);
Protect[SendMail]; SetAttributes[SendMail, Locked];

(* Cloud *)
Unprotect[CloudDeploy]; ClearAll[CloudDeploy];
CloudDeploy[___] := (Message[General::sandbox, "CloudDeploy"]; $Failed);
Protect[CloudDeploy]; SetAttributes[CloudDeploy, Locked];

Unprotect[CloudPut]; ClearAll[CloudPut];
CloudPut[___] := (Message[General::sandbox, "CloudPut"]; $Failed);
Protect[CloudPut]; SetAttributes[CloudPut, Locked];

(* Interop *)
Unprotect[ExternalEvaluate]; ClearAll[ExternalEvaluate];
ExternalEvaluate[___] := (Message[General::sandbox, "ExternalEvaluate"]; $Failed);
Protect[ExternalEvaluate]; SetAttributes[ExternalEvaluate, Locked];

Unprotect[StartExternalSession]; ClearAll[StartExternalSession];
StartExternalSession[___] := (Message[General::sandbox, "StartExternalSession"]; $Failed);
Protect[StartExternalSession]; SetAttributes[StartExternalSession, Locked];

]]; (* end Quiet/Block *)
