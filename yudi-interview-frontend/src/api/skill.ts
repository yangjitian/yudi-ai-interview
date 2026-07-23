import { request } from './request';

export interface CategoryDTO {
  key: string;
  label: string;
  priority: 'CORE' | 'NORMAL' | 'ALWAYS_ONE';
  ref?: string;
  shared?: boolean;
}

export interface DisplayDTO {
  icon: string;
  gradient: string;
  iconBg: string;
  iconColor: string;
}

export interface SkillDTO {
  id: string;
  name: string;
  description: string;
  categories: CategoryDTO[];
  isPreset: boolean;
  sourceJd: string | null;
  persona?: string;
  display?: DisplayDTO;
}

let skillsCache: SkillDTO[] | null = null;
let skillsPromise: Promise<SkillDTO[]> | null = null;

export const skillApi = {
  async listSkills(): Promise<SkillDTO[]> {
    if (skillsCache) return skillsCache;
    if (!skillsPromise) {
      skillsPromise = request.get<SkillDTO[]>('/api/skills')
        .then(data => {
          skillsCache = data;
          return data;
        })
        .finally(() => {
          skillsPromise = null;
        });
    }
    return skillsPromise;
  },

  async getSkill(id: string): Promise<SkillDTO> {
    return request.get<SkillDTO>(`/api/skills/${id}`);
  },

  async parseJd(jdText: string): Promise<CategoryDTO[]> {
    const text = jdText.toLowerCase();
    const categories: CategoryDTO[] = [];
    const keywords: [string, string][] = [
      ['java', 'Java'],
      ['spring', 'Spring'],
      ['mysql', 'MySQL'],
      ['redis', 'Redis'],
      ['kafka', 'Kafka'],
      ['docker', 'Docker'],
      ['kubernetes', 'K8s'],
      ['微服务', '微服务'],
      ['分布式', '分布式'],
      ['高并发', '高并发'],
      ['vue', 'Vue'],
      ['react', 'React'],
      ['javascript', 'JavaScript'],
      ['typescript', 'TypeScript'],
      ['python', 'Python'],
      ['django', 'Django'],
      ['fastapi', 'FastAPI'],
      ['golang', 'Go'],
      ['go语言', 'Go'],
    ];
    for (const [kw, label] of keywords) {
      if (text.includes(kw)) {
        categories.push({ key: kw, label, priority: 'NORMAL' });
      }
    }
    if (categories.length === 0) {
      categories.push({ key: 'general', label: '通用技能', priority: 'CORE' });
    }
    return categories;
  },
};
