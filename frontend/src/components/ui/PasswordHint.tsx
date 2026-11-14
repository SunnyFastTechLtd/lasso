import { PASSWORD_RULES } from '@/utils/password'
import { Icon } from './Icon'

/** 口令要求实时清单：用在新建账号、重置密码、修改本人密码三处 */
export function PasswordHint({ password, username }: { password: string; username?: string }) {
  return (
    <ul className="col-stack gap-[2px]">
      {PASSWORD_RULES.map((rule) => {
        const passed = rule.passed(password, username)
        return (
          <li
            key={rule.label}
            className={`flex items-center gap-[5px] text-xs ${
              passed ? 'text-state-success' : 'text-muted-foreground'
            }`}
          >
            <Icon name={passed ? 'Check' : 'Dot'} size={11} />
            {rule.label}
          </li>
        )
      })}
    </ul>
  )
}
