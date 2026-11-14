import {
  Activity, AlertTriangle, Archive, ArrowLeft, ArrowLeftRight, ArrowRight, BarChart3, BookOpen,
  Bot, Brain, Briefcase, Building2, Calculator, CalendarDays, ChartColumn, Check, ChevronDown,
  ChevronLeft, ChevronRight, ChevronsLeft, ChevronsRight, ChevronUp, CircleAlert, CircleCheck,
  CircleStop, CircleX, Copy, Cpu, Database, Dot, Download, ExternalLink, FileCog, FileDown,
  FileJson, FileQuestion, Files, FileText, FileUp, FileWarning, FileX, FlaskConical,
  FolderOpen, Gauge, GitCompare, Globe, GraduationCap,
  Hammer, HardDrive, Headset, HeartPulse, History, Inbox, Info, KeyRound, Layers, ListChecks,
  LoaderCircle,
  Lock, LogOut, MessageSquare, MessagesSquare, MonitorSmartphone, Palette, PanelLeftClose,
  PanelLeftOpen, Paperclip, PenTool, Pencil, Plug, PlugZap, Plus, Power, PowerOff, Puzzle,
  RefreshCw, Rocket, RotateCcw, RotateCw, Scale, ScrollText, Search, Send, Server, Settings,
  Shield, ShieldAlert, ShieldCheck, ShieldOff, SlidersHorizontal, Sparkles, Square, Stethoscope,
  Table2, Trash2, TriangleAlert,
  Upload, User, UserPlus, Users, Wrench, X, Zap,
} from 'lucide-react'
import type { LucideIcon } from 'lucide-react'

/**
 * 按需注册的图标表（只引入实际用到的图标）。
 *
 * 新增图标时：先在这里 import 并登记，再在业务里用 `name="..."`。
 * 名称对不上时 Icon 会渲染可见的占位圆点并带上 aria-label，不会静默空白。
 */
export const ICON_MAP: Record<string, LucideIcon> = {
  Activity, AlertTriangle, Archive, ArrowLeft, ArrowLeftRight, ArrowRight, BarChart3, BookOpen,
  Bot, Brain, Briefcase, Building2, Calculator, CalendarDays, ChartColumn, Check, ChevronDown,
  ChevronLeft, ChevronRight, ChevronsLeft, ChevronsRight, ChevronUp, CircleAlert, CircleCheck,
  CircleStop, CircleX, Copy, Cpu, Database, Dot, Download, ExternalLink, FileCog, FileDown,
  FileJson, FileQuestion, Files, FileText, FileUp, FileWarning, FileX, FlaskConical,
  FolderOpen, Gauge, GitCompare, Globe, GraduationCap,
  Hammer, HardDrive, Headset, HeartPulse, History, Inbox, Info, KeyRound, Layers, ListChecks,
  LoaderCircle,
  Lock, LogOut, MessageSquare, MessagesSquare, MonitorSmartphone, Palette, PanelLeftClose,
  PanelLeftOpen, Paperclip, PenTool, Pencil, Plug, PlugZap, Plus, Power, PowerOff, Puzzle,
  RefreshCw, Rocket, RotateCcw, RotateCw, Scale, ScrollText, Search, Send, Server, Settings,
  Shield, ShieldAlert, ShieldCheck, ShieldOff, SlidersHorizontal, Sparkles, Square, Stethoscope,
  Table2, Trash2, TriangleAlert,
  Upload, User, UserPlus, Users, Wrench, X, Zap,
}
